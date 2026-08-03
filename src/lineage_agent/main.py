"""RippleProof: generate a schema change with DataHub-grounded impact.

The current vertical slice intentionally supports one change class: renaming a
PostgreSQL column. Before generating SQL it verifies that the source column is
present in DataHub and reads that column's downstream lineage over MCP.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .campaign import ChangeBundle, build_change_bundle, parse_model_maps
from .llm import interpret_rename, is_llm_configured
from .reporting import render_markdown_report, write_markdown_report


DEFAULT_ASSET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
DEFAULT_INSTRUCTION = "rename user_id to customer_id"


@dataclass(frozen=True)
class ChangeRequest:
    """A requested change to a DataHub dataset."""

    asset_urn: str
    instruction: str


@dataclass(frozen=True)
class RenameChange:
    old_column: str
    new_column: str


@dataclass
class ImpactReport:
    """Tracked downstream impact returned by DataHub."""

    downstream: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


@dataclass
class GeneratedChange:
    change_sql: str
    impact: ImpactReport
    rename: RenameChange | None = None
    generator: str = "deterministic"
    schema_fields: tuple[str, ...] = ()
    max_hops: int | None = None
    context_source: str = "provided"
    source_owners: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class AssetContext:
    schema_fields: tuple[str, ...]
    downstream: tuple[dict[str, Any], ...]
    source_owners: tuple[dict[str, str], ...] = ()


def _configure_output() -> None:
    """Make MCP and report output safe on Windows terminals."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def _mcp_params() -> StdioServerParameters:
    load_dotenv()
    child_env = {
        **os.environ,
        # The MCP server logs full GraphQL documents at DEBUG by default. Keep
        # normal runs focused on the generated artifact and impact report.
        "LOGURU_LEVEL": os.environ.get("LOGURU_LEVEL", "WARNING"),
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_datahub"],
        env=child_env,
    )


def _tool_payload(result: Any, tool_name: str) -> Any:
    if getattr(result, "isError", False):
        detail = "".join(getattr(block, "text", "") for block in result.content)
        raise RuntimeError(f"DataHub MCP tool {tool_name!r} failed: {detail}")

    for block in result.content:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, (dict, list)):
            return payload

    raise RuntimeError(f"DataHub MCP tool {tool_name!r} returned no JSON value")


def _tool_json(result: Any, tool_name: str) -> dict[str, Any]:
    payload = _tool_payload(result, tool_name)
    if not isinstance(payload, dict):
        raise RuntimeError(f"DataHub MCP tool {tool_name!r} returned no JSON object")
    return payload


def _field_names(payload: dict[str, Any]) -> tuple[str, ...]:
    names = []
    for item in payload.get("fields", []):
        field_path = item.get("fieldPath")
        if isinstance(field_path, str):
            names.append(field_path)
    return tuple(names)


def _downstream_assets(
    payload: dict[str, Any], evidence: str
) -> tuple[dict[str, Any], ...]:
    container = payload.get("downstreams") or {}
    hits = payload.get("results")
    if not isinstance(hits, list):
        hits = container.get("searchResults", [])
    if not isinstance(hits, list):
        raise RuntimeError("DataHub lineage response did not contain a result list")
    reported_count = payload.get("downstreamCount", container.get("total"))
    if isinstance(reported_count, int) and reported_count > len(hits):
        raise RuntimeError(
            f"DataHub reported {reported_count} downstream result(s) but returned "
            f"only {len(hits)}. RippleProof refuses to plan from truncated lineage."
        )
    downstream = []
    for hit in hits:
        entity = hit.get("entity") or {}
        urn = entity.get("urn")
        entity_type = entity.get("type")
        if not urn or not entity_type:
            continue
        downstream.append(
            {
                "urn": urn,
                "type": entity_type,
                "name": entity.get("properties", {}).get("name")
                or entity.get("name")
                or urn,
                "platform": entity.get("platform", {}).get("name", "?"),
                "degree": hit.get("degree"),
                "columns": tuple(hit.get("lineageColumns", [])),
                "evidence": evidence,
            }
        )
    return tuple(downstream)


def _merge_downstream_assets(
    column_assets: tuple[dict[str, Any], ...],
    asset_level_assets: tuple[dict[str, Any], ...],
) -> tuple[dict[str, Any], ...]:
    """Prefer column evidence, then retain wider asset-level reachability."""
    merged = list(column_assets)
    seen = {asset["urn"] for asset in column_assets}
    merged.extend(asset for asset in asset_level_assets if asset["urn"] not in seen)
    return tuple(merged)


def _owner_name(owner: dict[str, Any]) -> str:
    for container_name in ("editableProperties", "properties", "editableInfo", "info"):
        container = owner.get(container_name) or {}
        for key in ("displayName", "name", "email"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    name = owner.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    urn = str(owner.get("urn") or "unassigned")
    return urn.rsplit(":", 1)[-1]


def _owners_by_asset(payload: Any) -> dict[str, tuple[dict[str, str], ...]]:
    entities = payload if isinstance(payload, list) else [payload]
    result: dict[str, tuple[dict[str, str], ...]] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        asset_urn = entity.get("urn")
        if not isinstance(asset_urn, str):
            continue
        owners = []
        for assignment in (entity.get("ownership") or {}).get("owners", []):
            owner = assignment.get("owner") or {}
            owner_urn = owner.get("urn")
            if not isinstance(owner_urn, str):
                continue
            ownership_type = assignment.get("ownershipType") or {}
            type_info = ownership_type.get("info") or {}
            owner_type = (
                type_info.get("name")
                or ownership_type.get("type")
                or assignment.get("type")
                or "owner"
            )
            owners.append(
                {
                    "urn": owner_urn,
                    "name": _owner_name(owner),
                    "type": str(owner_type),
                }
            )
        result[asset_urn] = tuple(owners)
    return result


def _attach_owners(
    assets: tuple[dict[str, Any], ...],
    owners_by_asset: dict[str, tuple[dict[str, str], ...]],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {**asset, "owners": owners_by_asset.get(str(asset.get("urn")), ())}
        for asset in assets
    )


async def read_asset_context(
    asset_urn: str,
    source_column: str,
    max_hops: int = 2,
    destination_column: str | None = None,
) -> AssetContext:
    """Validate a column and read its downstream lineage in one MCP session."""
    with open(os.devnull, "w", encoding="utf-8") as mcp_log:
        async with stdio_client(_mcp_params(), errlog=mcp_log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                schema_names: list[str] = []
                offset = 0
                while True:
                    schema_result = await session.call_tool(
                        "list_schema_fields",
                        {"urn": asset_urn, "limit": 100, "offset": offset},
                    )
                    schema_payload = _tool_json(schema_result, "list_schema_fields")
                    page_names = _field_names(schema_payload)
                    schema_names.extend(page_names)
                    remaining = schema_payload.get("remainingCount")
                    returned = schema_payload.get("returned")
                    if not isinstance(remaining, int) or not isinstance(returned, int):
                        raise RuntimeError(
                            "DataHub schema response did not contain pagination metadata; "
                            "RippleProof cannot prove the destination column is absent."
                        )
                    if returned != len(page_names):
                        raise RuntimeError(
                            "DataHub schema response field count did not match its payload."
                        )
                    if remaining <= 0:
                        break
                    if returned <= 0:
                        raise RuntimeError(
                            "DataHub reported more schema fields but returned no next page."
                        )
                    offset += returned
                schema_fields = tuple(schema_names)
                if source_column not in schema_fields:
                    available = ", ".join(schema_fields) if schema_fields else "none returned"
                    raise ValueError(
                        f"Column {source_column!r} is not present in DataHub's schema for "
                        f"{asset_urn}. Matching fields: {available}."
                    )
                if destination_column and destination_column in schema_fields:
                    raise ValueError(
                        f"Cannot rename {source_column!r} to {destination_column!r}: "
                        "the destination column already exists in DataHub's source schema."
                    )

                column_result = await session.call_tool(
                    "get_lineage",
                    {
                        "urn": asset_urn,
                        "column": source_column,
                        "upstream": False,
                        "max_hops": max_hops,
                        "max_results": 50,
                    },
                )
                column_assets = _downstream_assets(
                    _tool_json(column_result, "get_lineage"), "column"
                )

                asset_result = await session.call_tool(
                    "get_lineage",
                    {
                        "urn": asset_urn,
                        "upstream": False,
                        "max_hops": max_hops,
                        "max_results": 50,
                    },
                )
                asset_level_assets = _downstream_assets(
                    _tool_json(asset_result, "get_lineage"), "asset"
                )
                downstream = _merge_downstream_assets(column_assets, asset_level_assets)
                if downstream:
                    ownership_result = await session.call_tool(
                        "get_entities",
                        {"urns": [asset_urn, *(asset["urn"] for asset in downstream)]},
                    )
                    owners_by_asset = _owners_by_asset(
                        _tool_payload(ownership_result, "get_entities")
                    )
                    downstream = _attach_owners(downstream, owners_by_asset)
                else:
                    owners_by_asset = {}
                return AssetContext(
                    schema_fields=schema_fields,
                    downstream=downstream,
                    source_owners=owners_by_asset.get(asset_urn, ()),
                )


RENAME_RE = re.compile(
    r"^\s*rename\s+([A-Za-z_][A-Za-z0-9_$]*)\s+to\s+"
    r"([A-Za-z_][A-Za-z0-9_$]*)\s*$",
    re.IGNORECASE,
)
DATASET_URN_RE = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^)]+)\)$"
)


def parse_rename(instruction: str) -> RenameChange:
    match = RENAME_RE.match(instruction)
    if not match:
        raise ValueError(
            "This vertical slice supports only 'rename <column> to <column>'."
        )
    old_column, new_column = match.groups()
    if old_column == new_column:
        raise ValueError("The source and destination column names must differ.")
    return RenameChange(old_column=old_column, new_column=new_column)


def _postgres_table_name(asset_urn: str) -> str:
    match = DATASET_URN_RE.match(asset_urn)
    if not match:
        raise ValueError(f"Unsupported DataHub dataset URN: {asset_urn!r}")
    platform, dataset_name, _environment = match.groups()
    if platform.lower() != "postgres":
        raise ValueError(
            f"This vertical slice generates PostgreSQL SQL, not {platform!r} SQL."
        )
    return ".".join(_quote_identifier(part) for part in dataset_name.split("."))


def _quote_identifier(identifier: str) -> str:
    if not identifier:
        raise ValueError("SQL identifiers cannot be empty")
    return f'"{identifier.replace(chr(34), chr(34) * 2)}"'


def _remediation_action(asset: dict[str, Any], rename: RenameChange) -> str:
    """Recommend only what the asset's lineage evidence supports."""
    if asset.get("evidence") == "column":
        derived_columns = ", ".join(asset.get("columns") or ()) or "unknown"
        degree = asset.get("degree")
        if isinstance(degree, int) and degree > 1:
            return (
                "Treat this as a transitive consumer. Update it only when the "
                f"chosen migration propagates `{rename.new_column}` through the "
                "preceding model; leave it unchanged when a one-hop compatibility "
                f"boundary preserves `{rename.old_column}`. DataHub reports derived "
                f"column(s) `{derived_columns}`."
            )
        return (
            f"Update the upstream reference from `{rename.old_column}` to "
            f"`{rename.new_column}`. Confirm whether downstream column(s) "
            f"`{derived_columns}` should keep their current names or be renamed too."
        )
    return (
        f"Inspect this asset for references to `{rename.old_column}` before applying "
        "the change. DataHub confirms reachability, but column usage is unconfirmed."
    )


def _review_route(asset: dict[str, Any]) -> str:
    if "owners" not in asset:
        return ""
    owners = asset.get("owners") or ()
    if not owners:
        return " Review owner is unassigned in DataHub."
    names = ", ".join(str(owner.get("name") or owner.get("urn")) for owner in owners)
    return f" Route review to {names}."


def generate_change(
    request: ChangeRequest,
    rename: RenameChange,
    downstream: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    generator: str = "deterministic",
    *,
    schema_fields: tuple[str, ...] = (),
    max_hops: int | None = None,
    context_source: str = "provided",
    source_owners: tuple[dict[str, str], ...] = (),
) -> GeneratedChange:
    """Generate PostgreSQL for a validated rename and its impact report."""
    if rename.new_column in schema_fields:
        raise ValueError(
            f"Cannot rename {rename.old_column!r} to {rename.new_column!r}: "
            "the destination column already exists in DataHub's source schema."
        )
    table = _postgres_table_name(request.asset_urn)
    old_column = _quote_identifier(rename.old_column)
    new_column = _quote_identifier(rename.new_column)
    change_sql = f"ALTER TABLE {table} RENAME COLUMN {old_column} TO {new_column};"

    if downstream:
        column_count = sum(asset.get("evidence") == "column" for asset in downstream)
        asset_count = len(downstream) - column_count
        lines = []
        remediation_lines = []
        for asset in downstream:
            degree = asset.get("degree")
            hop_label = f"{degree} hop{'s' if degree != 1 else ''} away" if degree else "hop unknown"
            columns = asset.get("columns") or ()
            if asset.get("evidence") == "column":
                evidence_label = f"column-level; derived column(s): {', '.join(columns)}"
            else:
                evidence_label = "asset-level reachability; column usage unconfirmed"
            lines.append(
                f"  - {asset['type']} `{asset['name']}` "
                f"({asset['platform']}, {hop_label}; {evidence_label})"
            )
            remediation_lines.append(
                f"  - `{asset['name']}`: {_remediation_action(asset, rename)}"
                f"{_review_route(asset)}"
            )
        summary = (
            f"DataHub found {len(downstream)} downstream asset(s) for "
            f"`{rename.old_column}` ({column_count} column-level, "
            f"{asset_count} asset-level):\n"
            + "\n".join(lines)
            + "\n\nRemediation plan:\n"
            + "\n".join(remediation_lines)
            + "\n\nReview and update each tracked consumer before merging. "
            "This report cannot detect dependencies missing from DataHub."
        )
    else:
        summary = (
            f"DataHub returned no downstream column lineage for `{rename.old_column}`. "
            "This is not proof that the rename is safe: downstream dependencies may "
            "exist but be absent from the metadata graph."
        )

    return GeneratedChange(
        change_sql=change_sql,
        impact=ImpactReport(downstream=list(downstream), summary=summary),
        rename=rename,
        generator=generator,
        schema_fields=schema_fields,
        max_hops=max_hops,
        context_source=context_source,
        source_owners=source_owners,
    )


async def resolve_rename(
    request: ChangeRequest,
    generator: str,
    *,
    llm_client: Any | None = None,
) -> tuple[RenameChange, str]:
    """Resolve a request through the selected constrained parser."""
    if generator not in {"deterministic", "llm"}:
        raise ValueError(
            "Generator must be 'deterministic' or explicitly 'llm'. "
            "Ambient API keys never select a model."
        )
    if generator == "deterministic":
        return parse_rename(request.instruction), "deterministic parser"

    if generator == "llm" and not is_llm_configured() and llm_client is None:
        raise RuntimeError(
            "OPENAI_API_KEY is required for --generator llm. Configure it in .env "
            "or use --generator deterministic."
        )

    interpreted = await interpret_rename(
        request.instruction,
        request.asset_urn,
        client=llm_client,
    )
    return (
        RenameChange(interpreted.old_column, interpreted.new_column),
        f"OpenAI {interpreted.model} structured intent",
    )


async def run(
    request: ChangeRequest,
    max_hops: int = 2,
    generator: str = "deterministic",
    *,
    llm_client: Any | None = None,
) -> GeneratedChange:
    rename, generator_label = await resolve_rename(
        request,
        generator,
        llm_client=llm_client,
    )
    context = await read_asset_context(
        request.asset_urn,
        rename.old_column,
        max_hops,
        destination_column=rename.new_column,
    )
    return generate_change(
        request,
        rename,
        context.downstream,
        generator=generator_label,
        schema_fields=context.schema_fields,
        max_hops=max_hops,
        context_source="live-mcp",
        source_owners=context.source_owners,
    )


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PostgreSQL column rename with DataHub impact analysis."
    )
    parser.add_argument("--asset-urn", default=DEFAULT_ASSET_URN)
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    parser.add_argument("--max-hops", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument(
        "--generator",
        choices=("deterministic", "llm"),
        default="deterministic",
        help=(
            "strict local parser by default; model use occurs only with the explicit "
            "'llm' selection"
        ),
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--report-file",
        help="write the proposed SQL and DataHub impact analysis to Markdown",
    )
    parser.add_argument(
        "--target-repo",
        action="append",
        help=(
            "dbt project containing confirmed downstream models; repeat for "
            "multi-repository campaigns"
        ),
    )
    parser.add_argument(
        "--bundle-dir",
        help="new directory for migration, proposed models, diffs, report, and manifest",
    )
    parser.add_argument(
        "--model-map",
        action="append",
        default=[],
        help=(
            "explicitly resolve an ambiguous asset with "
            "DATAHUB_URN=repository/models/model.sql; repeat as needed"
        ),
    )
    parser.add_argument(
        "--downstream-mode",
        choices=("preserve", "propagate"),
        default="preserve",
        help="preserve confirmed downstream output names by default",
    )
    parser.add_argument(
        "--validate-dbt",
        action="store_true",
        help=(
            "execute dbt seed/build in disposable copied repositories with DuckDB; "
            "target-project macros run only when this explicit flag is set"
        ),
    )
    parser.add_argument(
        "--dbt-executable",
        default="dbt",
        help="dbt executable used by --validate-dbt",
    )
    parser.add_argument(
        "--validate-postgres",
        action="store_true",
        help=(
            "execute the constrained source rename and rollback in a disposable, "
            "networkless PostgreSQL container"
        ),
    )
    parser.add_argument(
        "--docker-executable",
        default="docker",
        help="Docker executable used by --validate-postgres",
    )
    parser.add_argument(
        "--postgres-image",
        default="postgres:16",
        help="locally available PostgreSQL image used by --validate-postgres",
    )
    args = parser.parse_args(argv)
    if bool(args.target_repo) != bool(args.bundle_dir):
        parser.error("--target-repo and --bundle-dir must be provided together")
    if args.validate_dbt and not args.target_repo:
        parser.error("--validate-dbt requires --target-repo and --bundle-dir")
    if args.validate_postgres and not args.target_repo:
        parser.error("--validate-postgres requires --target-repo and --bundle-dir")
    if args.model_map and not args.target_repo:
        parser.error("--model-map requires --target-repo and --bundle-dir")
    if args.write_back:
        parser.error(
            "Legacy --write-back is disabled because it is not manifest-bound. "
            "Create and validate a bundle, publish review-only pull requests, then "
            "use 'ripple-proof record-decision' with the exact manifest approval."
        )
    try:
        args.model_map = parse_model_maps(args.model_map)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def _known_error_message(exc: BaseException) -> str | None:
    """Find an expected domain/runtime error inside async ExceptionGroups."""
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            message = _known_error_message(nested)
            if message:
                return message
    return None


def main(argv: list[str] | None = None) -> None:
    _configure_output()
    args = _arguments(argv)
    request = ChangeRequest(asset_urn=args.asset_urn, instruction=args.instruction)
    report_path = None
    bundle: ChangeBundle | None = None
    try:
        result = asyncio.run(
            run(
                request,
                max_hops=args.max_hops,
                generator=args.generator,
            )
        )
        if args.target_repo:
            bundle = build_change_bundle(
                result=result,
                source_urn=request.asset_urn,
                instruction=request.instruction,
                target_repos=args.target_repo,
                output_dir=args.bundle_dir,
                downstream_mode=args.downstream_mode,
                model_maps=args.model_map,
                validate_dbt=args.validate_dbt,
                dbt_executable=args.dbt_executable,
                validate_postgres=args.validate_postgres,
                docker_executable=args.docker_executable,
                postgres_image=args.postgres_image,
            )
        if args.report_file:
            report_path = write_markdown_report(
                render_markdown_report(
                    result,
                    source_urn=request.asset_urn,
                    instruction=request.instruction,
                    artifact_summary=(
                        bundle.artifact_summary if bundle is not None else None
                    ),
                ),
                args.report_file,
            )
    except Exception as exc:
        message = _known_error_message(exc)
        if message is None:
            raise
        print(f"RippleProof could not generate the change: {message}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"Generator: {result.generator}\n")
    print(f"Generated change:\n  {result.change_sql}\n")
    print(f"Impact report:\n{result.impact.summary}")
    if report_path:
        print(f"\nMarkdown report: {report_path}")
    if bundle is not None:
        print(
            f"\nChange bundle: {bundle.output_dir} "
            f"({len(bundle.patches)} SQL patch(es), "
            f"{len(bundle.contract_patches)} contract patch(es), "
            f"{len(bundle.protected)} contract-protected asset(s), "
            f"{len(bundle.unresolved)} manual-review asset(s), "
            f"mode={bundle.downstream_mode}, "
            f"dbt_validation={bundle.dbt_validation.status if bundle.dbt_validation else 'not-requested'}, "
            f"postgres_validation={bundle.postgres_validation.status if bundle.postgres_validation else 'not-requested'}, "
            f"ready_for_review={str(bundle.ready_for_review).lower()})"
        )


if __name__ == "__main__":
    main()
