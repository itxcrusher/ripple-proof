"""BlastRadius: generate a schema change with DataHub-grounded impact.

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

from .llm import interpret_rename, is_llm_configured
from .reporting import render_markdown_report, write_markdown_report
from .writeback import write_impact_notes


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
    generator: str = "deterministic"


@dataclass(frozen=True)
class AssetContext:
    schema_fields: tuple[str, ...]
    downstream: tuple[dict[str, Any], ...]


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


def _tool_json(result: Any, tool_name: str) -> dict[str, Any]:
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
        if isinstance(payload, dict):
            return payload

    raise RuntimeError(f"DataHub MCP tool {tool_name!r} returned no JSON object")


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
    hits = payload.get("downstreams", {}).get("searchResults", [])
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


async def read_asset_context(
    asset_urn: str,
    source_column: str,
    max_hops: int = 2,
) -> AssetContext:
    """Validate a column and read its downstream lineage in one MCP session."""
    with open(os.devnull, "w", encoding="utf-8") as mcp_log:
        async with stdio_client(_mcp_params(), errlog=mcp_log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                schema_result = await session.call_tool(
                    "list_schema_fields",
                    {"urn": asset_urn, "keywords": [source_column], "limit": 100},
                )
                schema_fields = _field_names(_tool_json(schema_result, "list_schema_fields"))
                if source_column not in schema_fields:
                    available = ", ".join(schema_fields) if schema_fields else "none returned"
                    raise ValueError(
                        f"Column {source_column!r} is not present in DataHub's schema for "
                        f"{asset_urn}. Matching fields: {available}."
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
                return AssetContext(schema_fields=schema_fields, downstream=downstream)


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
        return (
            f"Update the upstream reference from `{rename.old_column}` to "
            f"`{rename.new_column}`. Confirm whether downstream column(s) "
            f"`{derived_columns}` should keep their current names or be renamed too."
        )
    return (
        f"Inspect this asset for references to `{rename.old_column}` before applying "
        "the change. DataHub confirms reachability, but column usage is unconfirmed."
    )


def generate_change(
    request: ChangeRequest,
    rename: RenameChange,
    downstream: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    generator: str = "deterministic",
) -> GeneratedChange:
    """Generate PostgreSQL for a validated rename and its impact report."""
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
        generator=generator,
    )


async def resolve_rename(
    request: ChangeRequest,
    generator: str,
    *,
    llm_client: Any | None = None,
) -> tuple[RenameChange, str]:
    """Resolve a request through the selected constrained parser."""
    use_llm = generator == "llm" or (generator == "auto" and is_llm_configured())
    if not use_llm:
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
    generator: str = "auto",
    *,
    llm_client: Any | None = None,
) -> GeneratedChange:
    rename, generator_label = await resolve_rename(
        request,
        generator,
        llm_client=llm_client,
    )
    context = await read_asset_context(request.asset_urn, rename.old_column, max_hops)
    return generate_change(
        request,
        rename,
        context.downstream,
        generator=generator_label,
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
        choices=("auto", "llm", "deterministic"),
        default="auto",
        help="auto uses OpenAI when configured, otherwise the strict local parser",
    )
    parser.add_argument(
        "--write-back",
        action="store_true",
        help="attach the impact report to each downstream asset in DataHub",
    )
    parser.add_argument(
        "--report-file",
        help="write the proposed SQL and DataHub impact analysis to Markdown",
    )
    return parser.parse_args(argv)


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
    written_urns: tuple[str, ...] = ()
    report_path = None
    try:
        result = asyncio.run(
            run(
                request,
                max_hops=args.max_hops,
                generator=args.generator,
            )
        )
        if args.write_back:
            written_urns = write_impact_notes(result, request.asset_urn)
        if args.report_file:
            report_path = write_markdown_report(
                render_markdown_report(
                    result,
                    source_urn=request.asset_urn,
                    instruction=request.instruction,
                ),
                args.report_file,
            )
    except Exception as exc:
        message = _known_error_message(exc)
        if message is None:
            raise
        print(f"BlastRadius could not generate the change: {message}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"Generator: {result.generator}\n")
    print(f"Generated change:\n  {result.change_sql}\n")
    print(f"Impact report:\n{result.impact.summary}")
    if args.write_back:
        print(
            f"\nDataHub write-back: attached the report to "
            f"{len(written_urns)} downstream asset(s)."
        )
    if report_path:
        print(f"\nMarkdown report: {report_path}")


if __name__ == "__main__":
    main()
