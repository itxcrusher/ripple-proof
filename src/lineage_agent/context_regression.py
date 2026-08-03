"""Exercise RippleProof context refusals against the controlled demo graph."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .context_guard import (
    ContextGuardError,
    ContextVerification,
    captured_context_from_manifest,
    replace_context_verification_evidence,
    require_fresh_context,
    verify_fresh_context,
)
from .lineage_freshness import read_cache_bypassed_lineage
from .main import DEFAULT_ASSET_URN
from .mutation_safety import (
    MutationSafetyError,
    NON_LOOPBACK_APPROVAL_TOKEN,
    require_datahub_mutation_permission,
)


DEMO_DOWNSTREAM_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)"
)
DEMO_SECOND_OWNER_URN = "urn:li:corpGroup:operations-data"


class ContextRegressionError(ValueError):
    """The controlled regression could not be completed or safely restored."""


def _load_captured(bundle: Path):
    manifest_path = bundle / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextRegressionError(f"Could not read campaign manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ContextRegressionError("Campaign manifest must contain a JSON object")
    captured = captured_context_from_manifest(manifest)
    downstream = {
        item.get("urn")
        for item in (captured.snapshot.get("lineage") or {}).get("downstream", ())
        if isinstance(item, dict)
    }
    if (
        captured.source_urn != DEFAULT_ASSET_URN
        or captured.source_column != "user_id"
        or DEMO_DOWNSTREAM_URN not in downstream
    ):
        raise ContextRegressionError(
            "This command is restricted to the checked analytics.orders demo graph"
        )
    return captured


def _emit_aspect(graph: Any, urn: str, aspect: Any) -> None:
    from datahub.emitter.mcp import MetadataChangeProposalWrapper

    graph.emit_mcp(MetadataChangeProposalWrapper(entityUrn=urn, aspect=aspect))


def _new_mutation_graph(endpoint: str) -> Any:
    from datahub.ingestion.graph.client import DataHubGraph
    from datahub.ingestion.graph.config import DatahubClientConfig

    return DataHubGraph(
        DatahubClientConfig(
            server=endpoint,
            token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
            datahub_component="ripple-proof-context-regression",
        )
    )


async def _wait_for_lineage_edge(
    *,
    present: bool,
    max_hops: int,
    timeout_seconds: int,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        observation = await read_cache_bypassed_lineage(
            DEFAULT_ASSET_URN,
            "user_id",
            max_hops,
        )
        column_urns = {item["urn"] for item in observation.column_downstream}
        asset_urns = {item["urn"] for item in observation.asset_downstream}
        observed = (
            DEMO_DOWNSTREAM_URN in column_urns
            and DEMO_DOWNSTREAM_URN in asset_urns
        )
        if observed is present:
            return
        if time.monotonic() >= deadline:
            state = "appear" if present else "disappear"
            raise ContextRegressionError(
                f"Demo lineage did not {state} in the cache-bypassed read within "
                f"{timeout_seconds} seconds"
            )
        await asyncio.sleep(2)


async def _verify_until(
    captured: Any,
    predicate: Callable[[ContextVerification], bool],
    *,
    timeout_seconds: int,
    label: str,
) -> ContextVerification:
    deadline = time.monotonic() + timeout_seconds
    last: ContextVerification | None = None
    while True:
        last = await verify_fresh_context(captured)
        if predicate(last):
            return last
        if time.monotonic() >= deadline:
            detail = "; ".join(last.differences) or "no difference observed"
            raise ContextRegressionError(
                f"Timed out waiting for {label}: {detail}"
            )
        await asyncio.sleep(2)


async def run_regression(
    bundle: Path,
    *,
    timeout_seconds: int,
    record_evidence: bool,
    allow_non_loopback_datahub_mutation: bool = False,
    non_loopback_approval_token: str | None = None,
) -> dict[str, ContextVerification]:
    datahub_url = require_datahub_mutation_permission(
        allow_non_loopback=allow_non_loopback_datahub_mutation,
        approval_token=non_loopback_approval_token,
    )
    from datahub.metadata.schema_classes import (
        OwnerClass,
        OwnershipClass,
        UpstreamLineageClass,
    )

    captured = _load_captured(bundle)
    print("[1/5] Verifying the unchanged demo graph with cache bypass...")
    matched = await _verify_until(
        captured,
        lambda result: result.matched
        and result.lineage_freshness is not None
        and result.lineage_freshness.matched,
        timeout_seconds=timeout_seconds,
        label="an unchanged cache-bypassed context match",
    )
    require_fresh_context(captured, matched)

    graph = _new_mutation_graph(datahub_url)
    try:
        original_ownership = graph.get_aspect(DEFAULT_ASSET_URN, OwnershipClass)
        original_lineage = graph.get_aspect(DEMO_DOWNSTREAM_URN, UpstreamLineageClass)
        if original_ownership is None or original_lineage is None:
            raise ContextRegressionError(
                "The controlled demo graph is missing ownership or lineage aspects"
            )
        owner_urns = {owner.owner for owner in original_ownership.owners}
        upstream_urns = {edge.dataset for edge in original_lineage.upstreams}
        if DEMO_SECOND_OWNER_URN in owner_urns or DEFAULT_ASSET_URN not in upstream_urns:
            raise ContextRegressionError(
                "The controlled demo graph is not in its expected baseline state"
            )

        print("[2/5] Adding one temporary source owner and expecting refusal...")
        changed_ownership = copy.deepcopy(original_ownership)
        changed_ownership.owners.append(
            OwnerClass(owner=DEMO_SECOND_OWNER_URN, type="TECHNICAL_OWNER")
        )
        try:
            _emit_aspect(graph, DEFAULT_ASSET_URN, changed_ownership)
            owner_refused = await _verify_until(
                captured,
                lambda result: "source ownership changed" in result.differences,
                timeout_seconds=timeout_seconds,
                label="owner-drift refusal",
            )
        finally:
            print("[3/5] Restoring the original source ownership...")
            _emit_aspect(graph, DEFAULT_ASSET_URN, original_ownership)

        print("[4/5] Removing one temporary lineage edge and expecting refusal...")
        removed_lineage = UpstreamLineageClass(upstreams=[], fineGrainedLineages=[])
        try:
            _emit_aspect(graph, DEMO_DOWNSTREAM_URN, removed_lineage)
            await _wait_for_lineage_edge(
                present=False,
                max_hops=captured.max_hops,
                timeout_seconds=timeout_seconds,
            )
            lineage_refused = await _verify_until(
                captured,
                lambda result: not result.matched
                and any(
                    "lineage" in difference or "downstream" in difference
                    for difference in result.differences
                ),
                timeout_seconds=timeout_seconds,
                label="removed-lineage refusal",
            )
        finally:
            print("[5/5] Restoring the original lineage and confirming the graph...")
            _emit_aspect(graph, DEMO_DOWNSTREAM_URN, original_lineage)
            await _wait_for_lineage_edge(
                present=True,
                max_hops=captured.max_hops,
                timeout_seconds=timeout_seconds,
            )
            restored = await _verify_until(
                captured,
                lambda result: result.matched
                and result.lineage_freshness is not None
                and result.lineage_freshness.matched,
                timeout_seconds=timeout_seconds,
                label="the fully restored demo context",
            )
            require_fresh_context(captured, restored)
    finally:
        close = getattr(graph, "close", None)
        if close:
            close()

    results = {
        "matched": matched,
        "refused-drift": owner_refused,
        "refused-lineage-drift": lineage_refused,
    }
    if record_evidence:
        path = replace_context_verification_evidence(
            bundle,
            captured=captured,
            verifications=results,
        )
        print(f"Evidence replaced atomically: {path}")
    return results


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Temporarily change the controlled local demo graph, prove owner and "
            "lineage refusals, and restore both aspects."
        )
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--record-evidence", action="store_true")
    parser.add_argument("--confirm-demo-mutation", action="store_true")
    parser.add_argument(
        "--allow-non-loopback-datahub-mutation",
        action="store_true",
        help="allow this destructive regression to target a non-loopback DataHub",
    )
    parser.add_argument(
        "--non-loopback-approval-token",
        help=(
            "exact non-loopback mutation approval token (required with the override): "
            f"{NON_LOOPBACK_APPROVAL_TOKEN}"
        ),
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    if not args.confirm_demo_mutation:
        parser.error(
            "--confirm-demo-mutation is required; this temporarily changes and restores "
            "the exact local demo ownership and lineage aspects"
        )
    if args.timeout < 30:
        parser.error("--timeout must be at least 30 seconds")
    try:
        args.datahub_url = require_datahub_mutation_permission(
            allow_non_loopback=args.allow_non_loopback_datahub_mutation,
            approval_token=args.non_loopback_approval_token,
        )
    except MutationSafetyError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    try:
        results = asyncio.run(
            run_regression(
                Path(args.bundle).expanduser().resolve(),
                timeout_seconds=args.timeout,
                record_evidence=args.record_evidence,
                allow_non_loopback_datahub_mutation=(
                    args.allow_non_loopback_datahub_mutation
                ),
                non_loopback_approval_token=args.non_loopback_approval_token,
            )
        )
    except (ContextGuardError, ContextRegressionError, MutationSafetyError) as exc:
        print(f"RippleProof context regression failed: {exc}")
        raise SystemExit(2) from exc
    print("Context regression: PASSED")
    print("  Unchanged graph: cache-bypassed match")
    print("  Owner change: refused")
    print("  Removed lineage: refused")
    print("  Demo graph: restored")
    if any(result.matched for key, result in results.items() if key != "matched"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
