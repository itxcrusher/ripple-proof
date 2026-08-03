"""Record an approved-for-review campaign decision in DataHub."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    PropertyValueClass,
    StructuredPropertiesClass,
    StructuredPropertyDefinitionClass,
    StructuredPropertyValueAssignmentClass,
)
from dotenv import load_dotenv

from .context_guard import (
    CapturedContext,
    ContextVerification,
    captured_context_from_manifest,
    require_fresh_context,
    verify_fresh_context,
    validate_lineage_freshness_proof,
)
from .replay import validate_publication_evidence


RIPPLE_PROOF_ACTOR_URN = "urn:li:corpuser:ripple-proof"
PROPERTY_PREFIX = "io.github.itxcrusher.ripple_proof."
PROPERTY_URNS = {
    "manifest": f"urn:li:structuredProperty:{PROPERTY_PREFIX}campaignManifestSha256",
    "decision": f"urn:li:structuredProperty:{PROPERTY_PREFIX}reviewDecision",
    "approved_at": f"urn:li:structuredProperty:{PROPERTY_PREFIX}approvedAt",
    "source_change": f"urn:li:structuredProperty:{PROPERTY_PREFIX}sourceChange",
    "merge_policy": f"urn:li:structuredProperty:{PROPERTY_PREFIX}mergePolicy",
    "pull_requests": f"urn:li:structuredProperty:{PROPERTY_PREFIX}pullRequests",
}
DECISION_STATUS = "approved-for-review"
MERGE_POLICY = "manual-only"


class DecisionError(RuntimeError):
    """A campaign decision could not be safely recorded or verified."""


@dataclass(frozen=True)
class DecisionPlan:
    bundle: Path
    manifest_sha256: str
    approved_at: str
    asset_urns: tuple[str, ...]
    values: Mapping[str, tuple[str, ...]]
    datahub_context: CapturedContext

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaignManifestSha256": self.manifest_sha256,
            "decision": DECISION_STATUS,
            "approvedAt": self.approved_at,
            "mergePolicy": MERGE_POLICY,
            "assets": list(self.asset_urns),
            "properties": {
                PROPERTY_URNS[key]: list(values)
                for key, values in self.values.items()
            },
        }


@dataclass(frozen=True)
class DecisionReadback:
    plan: DecisionPlan
    verified_asset_urns: tuple[str, ...]
    context_verification: ContextVerification

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            **self.plan.to_dict(),
            "freshSessionReadback": True,
            "dataHubContextVerification": self.context_verification.to_dict(),
            "verifiedAssets": list(self.verified_asset_urns),
        }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_object(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise ValueError(f"{label} does not exist: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value, raw


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Campaign evidence must provide {label}")
    return " ".join(value.split())


def _approved_at(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("approved_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("approved_at must include a timezone")
    return parsed.isoformat()


def prepare_decision(
    bundle_dir: str | Path,
    *,
    approved_manifest_sha256: str | None = None,
    approved_at: str | None = None,
) -> DecisionPlan:
    """Validate campaign evidence and create a read-only decision plan."""
    bundle = Path(bundle_dir).expanduser().resolve()
    manifest, manifest_bytes = _json_object(bundle / "manifest.json", "Manifest")
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    datahub_context = captured_context_from_manifest(manifest)
    if approved_manifest_sha256 is not None and approved_manifest_sha256 != manifest_sha256:
        raise DecisionError(
            "Approval hash does not match the campaign manifest: "
            f"expected {manifest_sha256}, received {approved_manifest_sha256}"
        )
    if not manifest.get("readyForReview"):
        raise ValueError("Campaign is not ready for review")
    failed = [
        item.get("id", "unknown")
        for item in manifest.get("validations") or []
        if isinstance(item, dict) and item.get("status") == "failed"
    ]
    if failed:
        raise ValueError(f"Campaign has failed validation gates: {', '.join(failed)}")
    validation = manifest.get("dbtValidation") or {}
    if not isinstance(validation, dict) or validation.get("status") != "passed":
        raise ValueError("Campaign must pass executable dbt validation")

    publication, _ = _json_object(bundle / "publication.json", "Publication evidence")
    publication = validate_publication_evidence(
        manifest,
        publication,
        campaign_manifest_sha256=manifest_sha256,
    )
    if publication.get("status") != "passed":
        raise ValueError("Published pull requests have not passed their required checks")
    if publication.get("mergePolicy") != MERGE_POLICY:
        raise ValueError("Publication evidence must use a manual-only merge policy")
    if publication.get("autoMergeEnabled") is not False:
        raise ValueError("Publication evidence must confirm that auto-merge is disabled")
    publication_context = publication.get("dataHubContextVerification") or {}
    try:
        publication_lineage = validate_lineage_freshness_proof(
            publication_context.get("lineageFreshness")
            if isinstance(publication_context, dict)
            else None
        )
    except ValueError as exc:
        raise ValueError(
            "Publication evidence must contain matching cache-bypassed lineage proof"
        ) from exc
    if (
        not isinstance(publication_context, dict)
        or publication_context.get("status") != "matched"
        or publication_context.get("capturedSha256")
        != datahub_context.fingerprint_sha256
        or publication_context.get("freshSha256")
        != datahub_context.fingerprint_sha256
        or not publication_lineage["matched"]
    ):
        raise ValueError(
            "Publication evidence must contain a matching DataHub context verification"
        )

    repositories = publication.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("Publication evidence must contain at least one repository")
    pull_requests: list[str] = []
    for repository in repositories:
        if not isinstance(repository, dict):
            raise ValueError("Publication evidence contains an invalid repository")
        pull_request = repository.get("pullRequest") or {}
        checks = repository.get("checks") or {}
        if (
            not isinstance(pull_request, dict)
            or pull_request.get("state") != "OPEN"
            or pull_request.get("autoMergeRequest") is not None
            or checks.get("status") != "passed"
        ):
            raise ValueError(
                "Every decision pull request must be open, passing, and manual-merge only"
            )
        pull_requests.append(_required_string(pull_request.get("url"), "pull request URL"))

    source = manifest.get("source") or {}
    rename = source.get("rename") or {}
    dataset = _required_string(source.get("dataset"), "source dataset")
    old_column = _required_string(rename.get("oldColumn"), "old column")
    new_column = _required_string(rename.get("newColumn"), "new column")
    source_urn = _required_string(source.get("urn"), "source URN")

    lineage_assets = manifest.get("lineageAssets") or []
    if not isinstance(lineage_assets, list):
        raise ValueError("Campaign lineageAssets must contain a list")
    asset_urns = [source_urn]
    for asset in lineage_assets:
        if not isinstance(asset, dict):
            raise ValueError("Campaign contains an invalid lineage asset")
        urn = _required_string(asset.get("urn"), "lineage asset URN")
        if urn not in asset_urns:
            asset_urns.append(urn)

    decision_timestamp = _approved_at(
        approved_at
        or _required_string(publication.get("publishedAt"), "publication timestamp")
    )
    return DecisionPlan(
        bundle=bundle,
        manifest_sha256=manifest_sha256,
        approved_at=decision_timestamp,
        asset_urns=tuple(asset_urns),
        values={
            "manifest": (manifest_sha256,),
            "decision": (DECISION_STATUS,),
            "approved_at": (decision_timestamp,),
            "source_change": (f"{dataset}.{old_column} -> {new_column}",),
            "merge_policy": (MERGE_POLICY,),
            "pull_requests": tuple(sorted(pull_requests)),
        },
        datahub_context=datahub_context,
    )


def _definitions() -> tuple[MetadataChangeProposalWrapper, ...]:
    entity_types = [
        "urn:li:entityType:datahub.dataset",
        "urn:li:entityType:datahub.dashboard",
    ]
    definitions = (
        ("manifest", "Campaign manifest SHA-256", "Exact approved campaign identity", None, None),
        (
            "decision",
            "Review decision",
            "RippleProof campaign decision; approval does not authorize merge or execution",
            [PropertyValueClass(value=DECISION_STATUS)],
            None,
        ),
        ("approved_at", "Approved at", "ISO-8601 review approval timestamp", None, None),
        ("source_change", "Source change", "Reviewed source column rename", None, None),
        (
            "merge_policy",
            "Merge policy",
            "Required merge policy for generated pull requests",
            [PropertyValueClass(value=MERGE_POLICY)],
            None,
        ),
        (
            "pull_requests",
            "Repair pull requests",
            "Generated pull requests covered by this review decision",
            None,
            "MULTIPLE",
        ),
    )
    return tuple(
        MetadataChangeProposalWrapper(
            entityUrn=PROPERTY_URNS[key],
            aspect=StructuredPropertyDefinitionClass(
                qualifiedName=PROPERTY_URNS[key].removeprefix(
                    "urn:li:structuredProperty:"
                ),
                displayName=display_name,
                description=description,
                valueType="urn:li:dataType:datahub.string",
                entityTypes=entity_types,
                allowedValues=allowed_values,
                cardinality=cardinality,
                immutable=False,
            ),
        )
        for key, display_name, description, allowed_values, cardinality in definitions
    )


def _assignments(
    plan: DecisionPlan,
    existing: StructuredPropertiesClass | None,
    timestamp_ms: int,
) -> StructuredPropertiesClass:
    previous = {item.propertyUrn: item for item in existing.properties} if existing else {}
    unrelated = [
        item
        for item in (existing.properties if existing else [])
        if not item.propertyUrn.startswith(
            f"urn:li:structuredProperty:{PROPERTY_PREFIX}"
        )
    ]
    stamp = AuditStampClass(time=timestamp_ms, actor=RIPPLE_PROOF_ACTOR_URN)
    ours = []
    for key, values in plan.values.items():
        prior = previous.get(PROPERTY_URNS[key])
        ours.append(
            StructuredPropertyValueAssignmentClass(
                propertyUrn=PROPERTY_URNS[key],
                values=list(values),
                created=prior.created if prior and prior.created else stamp,
                lastModified=stamp if prior else None,
            )
        )
    return StructuredPropertiesClass(properties=[*unrelated, *ours])


def _new_graph() -> DataHubGraph:
    load_dotenv()
    return DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
            datahub_component="ripple-proof",
        )
    )


def _close_graph(graph: Any) -> None:
    close = getattr(graph, "close", None)
    if close:
        close()


def write_approved_decision(
    plan: DecisionPlan,
    *,
    graph_factory: Callable[[], Any] = _new_graph,
    timestamp_ms: int | None = None,
    context_verification: ContextVerification | None = None,
) -> DecisionReadback:
    """Write a decision, then verify it through a fresh DataHub client."""
    require_fresh_context(plan.datahub_context, context_verification)
    if not plan.asset_urns:
        raise DecisionError("A decision must cover at least one DataHub asset")
    applied_at = (
        timestamp_ms
        if timestamp_ms is not None
        else int(datetime.now(timezone.utc).timestamp() * 1000)
    )
    writer = graph_factory()
    try:
        for proposal in _definitions():
            writer.emit_mcp(proposal)
        for urn in plan.asset_urns:
            existing = writer.get_aspect(urn, StructuredPropertiesClass)
            writer.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=urn,
                    aspect=_assignments(plan, existing, applied_at),
                )
            )
    except Exception as exc:
        raise DecisionError(f"DataHub decision write failed: {exc}") from exc
    finally:
        _close_graph(writer)

    reader = graph_factory()
    verified = []
    try:
        for urn in plan.asset_urns:
            aspect = reader.get_aspect(urn, StructuredPropertiesClass)
            actual = {
                item.propertyUrn: tuple(str(value) for value in item.values)
                for item in (aspect.properties if aspect else [])
                if item.propertyUrn in PROPERTY_URNS.values()
            }
            expected = {
                PROPERTY_URNS[key]: values for key, values in plan.values.items()
            }
            if actual != expected:
                raise DecisionError(
                    f"Fresh-session readback did not match for {urn}: "
                    f"expected {expected}, received {actual}"
                )
            verified.append(urn)
    except DecisionError:
        raise
    except Exception as exc:
        raise DecisionError(f"DataHub decision readback failed: {exc}") from exc
    finally:
        _close_graph(reader)

    return DecisionReadback(
        plan=plan,
        verified_asset_urns=tuple(verified),
        context_verification=context_verification,
    )


def write_decision_evidence(readback: DecisionReadback) -> Path:
    path = readback.plan.bundle / "decision.json"
    path.write_text(
        json.dumps(readback.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or write an approved-for-review campaign decision in DataHub."
    )
    parser.add_argument("--bundle-dir", required=True)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--approve-manifest-sha256")
    args = parser.parse_args(argv)
    if args.write and not args.approve_manifest_sha256:
        parser.error("--write requires --approve-manifest-sha256 from a dry-run")
    if not args.write and args.approve_manifest_sha256:
        parser.error("--approve-manifest-sha256 is only valid with --write")
    return args


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _arguments(argv)
    try:
        plan = prepare_decision(
            args.bundle_dir,
            approved_manifest_sha256=args.approve_manifest_sha256,
        )
        if not args.write:
            print("DataHub decision dry-run")
            print(f"  Manifest SHA-256: {plan.manifest_sha256}")
            print(f"  Decision: {DECISION_STATUS}")
            print(f"  Merge policy: {MERGE_POLICY}")
            print(f"  Assets: {len(plan.asset_urns)}")
            print("\nNo DataHub metadata was changed.")
            return
        context_verification = asyncio.run(verify_fresh_context(plan.datahub_context))
        readback = write_approved_decision(
            plan,
            context_verification=context_verification,
        )
        evidence = write_decision_evidence(readback)
    except (DecisionError, ValueError) as exc:
        print(f"RippleProof could not record the decision: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("DataHub decision recorded and independently read back")
    print(f"  Manifest SHA-256: {plan.manifest_sha256}")
    print(f"  Decision: {DECISION_STATUS}")
    print(f"  Verified assets: {len(readback.verified_asset_urns)}")
    print(f"  Evidence: {evidence}")


if __name__ == "__main__":
    main()
