"""Capture and revalidate the DataHub context behind a change campaign."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .lineage_freshness import CACHE_BYPASS_METHOD


FINGERPRINT_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 2
LEGACY_EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_SCENARIOS = ("matched", "refused-drift", "refused-lineage-drift")


class ContextGuardError(ValueError):
    """A campaign is missing, corrupt, or stale DataHub context."""


@dataclass(frozen=True)
class CapturedContext:
    source_urn: str
    source_column: str
    max_hops: int
    fingerprint_sha256: str
    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class LineageFreshnessProof:
    method: str
    skip_cache: bool
    mcp_column_sha256: str
    uncached_column_sha256: str
    mcp_asset_sha256: str
    uncached_asset_sha256: str
    differences: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return (
            self.method == CACHE_BYPASS_METHOD
            and self.skip_cache
            and self.mcp_column_sha256 == self.uncached_column_sha256
            and self.mcp_asset_sha256 == self.uncached_asset_sha256
            and not self.differences
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "skipCache": self.skip_cache,
            "matched": self.matched,
            "mcpColumnSha256": self.mcp_column_sha256,
            "uncachedColumnSha256": self.uncached_column_sha256,
            "mcpAssetSha256": self.mcp_asset_sha256,
            "uncachedAssetSha256": self.uncached_asset_sha256,
            "differences": list(self.differences),
        }


@dataclass(frozen=True)
class ContextVerification:
    captured_sha256: str
    fresh_sha256: str
    differences: tuple[str, ...]
    verified_at: str
    fresh_snapshot: Mapping[str, Any] | None = None
    lineage_freshness: LineageFreshnessProof | None = None

    @property
    def matched(self) -> bool:
        return self.captured_sha256 == self.fresh_sha256 and not self.differences

    def to_dict(self) -> dict[str, Any]:
        result = {
            "status": "matched" if self.matched else "drifted",
            "capturedSha256": self.captured_sha256,
            "freshSha256": self.fresh_sha256,
            "verifiedAt": self.verified_at,
            "differences": list(self.differences),
        }
        if self.lineage_freshness is not None:
            result["lineageFreshness"] = self.lineage_freshness.to_dict()
        return result


def _owners(values: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    normalized = {
        (
            str(owner.get("urn") or ""),
            str(owner.get("name") or ""),
            str(owner.get("type") or ""),
        )
        for owner in values
        if isinstance(owner, Mapping)
    }
    return [
        {"urn": urn, "name": name, "type": owner_type}
        for urn, name, owner_type in sorted(normalized)
    ]


def canonical_context_snapshot(
    *,
    source_urn: str,
    source_column: str,
    max_hops: int,
    schema_fields: Iterable[str],
    source_owners: Iterable[Mapping[str, Any]],
    downstream: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the stable metadata subset that authorizes campaign review actions."""
    assets: dict[str, dict[str, Any]] = {}
    for asset in downstream:
        urn = str(asset.get("urn") or "")
        if not urn:
            continue
        assets[urn] = {
            "urn": urn,
            "type": str(asset.get("type") or ""),
            "name": str(asset.get("name") or urn),
            "platform": str(asset.get("platform") or ""),
            "degree": asset.get("degree") if isinstance(asset.get("degree"), int) else None,
            "columns": sorted({str(value) for value in (asset.get("columns") or ())}),
            "evidence": str(asset.get("evidence") or ""),
            "owners": _owners(asset.get("owners") or ()),
        }
    return {
        "source": {
            "urn": source_urn,
            "column": source_column,
            "schemaFields": sorted({str(value) for value in schema_fields}),
            "owners": _owners(source_owners),
        },
        "lineage": {
            "maxHops": max_hops,
            "downstream": [assets[urn] for urn in sorted(assets)],
        },
    }


def context_fingerprint(snapshot: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def context_manifest_record(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "version": FINGERPRINT_VERSION,
        "algorithm": "sha256",
        "sha256": context_fingerprint(snapshot),
        "snapshot": snapshot,
    }


def _value_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _lineage_signature(
    downstream: Iterable[Mapping[str, Any]],
    *,
    include_columns: bool,
    confirmed_only: bool = False,
) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for asset in downstream:
        if confirmed_only and asset.get("evidence") != "column":
            continue
        urn = asset.get("urn")
        if not isinstance(urn, str) or not urn:
            continue
        record: dict[str, Any] = {
            "urn": urn,
            "type": str(asset.get("type") or ""),
            "degree": asset.get("degree") if isinstance(asset.get("degree"), int) else None,
        }
        if include_columns:
            record["columns"] = sorted(
                {str(column) for column in (asset.get("columns") or ())}
            )
        records[urn] = record
    return [records[urn] for urn in sorted(records)]


def _signature_difference(
    label: str,
    mcp: list[dict[str, Any]],
    uncached: list[dict[str, Any]],
) -> str | None:
    expected = {record["urn"]: record for record in mcp}
    actual = {record["urn"]: record for record in uncached}
    added = sorted(set(actual) - set(expected))
    removed = sorted(set(expected) - set(actual))
    changed = sorted(
        urn
        for urn in set(expected) & set(actual)
        if expected[urn] != actual[urn]
    )
    if not (added or removed or changed):
        return None
    return (
        f"cache-bypassed {label} lineage differs from MCP "
        f"(added: {', '.join(added) or 'none'}; "
        f"removed: {', '.join(removed) or 'none'}; "
        f"changed: {', '.join(changed) or 'none'})"
    )


def _lineage_freshness_proof(
    downstream: Iterable[Mapping[str, Any]],
    observation: Any,
) -> LineageFreshnessProof:
    mcp_column = _lineage_signature(
        downstream,
        include_columns=True,
        confirmed_only=True,
    )
    mcp_asset = _lineage_signature(downstream, include_columns=False)
    uncached_column = _lineage_signature(
        getattr(observation, "column_downstream", ()),
        include_columns=True,
    )
    uncached_asset = _lineage_signature(
        getattr(observation, "asset_downstream", ()),
        include_columns=False,
    )
    differences = tuple(
        difference
        for difference in (
            _signature_difference("column", mcp_column, uncached_column),
            _signature_difference("asset", mcp_asset, uncached_asset),
        )
        if difference is not None
    )
    return LineageFreshnessProof(
        method=str(getattr(observation, "method", "")),
        skip_cache=getattr(observation, "skip_cache", None) is True,
        mcp_column_sha256=_value_sha256(mcp_column),
        uncached_column_sha256=_value_sha256(uncached_column),
        mcp_asset_sha256=_value_sha256(mcp_asset),
        uncached_asset_sha256=_value_sha256(uncached_asset),
        differences=differences,
    )


def validate_lineage_freshness_proof(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextGuardError(
            "Context evidence is missing its cache-bypassed lineage proof"
        )
    hashes = (
        value.get("mcpColumnSha256"),
        value.get("uncachedColumnSha256"),
        value.get("mcpAssetSha256"),
        value.get("uncachedAssetSha256"),
    )
    differences = value.get("differences")
    if (
        value.get("method") != CACHE_BYPASS_METHOD
        or value.get("skipCache") is not True
        or not all(
            isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
            for digest in hashes
        )
        or not isinstance(differences, list)
        or not all(isinstance(item, str) and item for item in differences)
    ):
        raise ContextGuardError(
            "Context evidence contains an invalid cache-bypassed lineage proof"
        )
    matched = hashes[0] == hashes[1] and hashes[2] == hashes[3] and not differences
    if value.get("matched") is not matched:
        raise ContextGuardError(
            "Context evidence lineage freshness result is inconsistent"
        )
    return {
        "method": CACHE_BYPASS_METHOD,
        "skipCache": True,
        "matched": matched,
        "mcpColumnSha256": hashes[0],
        "uncachedColumnSha256": hashes[1],
        "mcpAssetSha256": hashes[2],
        "uncachedAssetSha256": hashes[3],
        "differences": list(differences),
    }


def _manifest_sha256(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def validate_context_verification_evidence(
    value: Mapping[str, Any],
    *,
    campaign_manifest_sha256: str,
    captured: CapturedContext,
) -> dict[str, Any]:
    """Validate a manifest-bound context verification sidecar."""
    schema_version = value.get("schemaVersion")
    if schema_version not in {
        LEGACY_EVIDENCE_SCHEMA_VERSION,
        EVIDENCE_SCHEMA_VERSION,
    }:
        raise ContextGuardError("Context evidence uses an unsupported schema version")
    if value.get("campaignManifestSha256") != campaign_manifest_sha256:
        raise ContextGuardError("Context evidence does not match the campaign manifest")
    if value.get("fingerprintVersion") != FINGERPRINT_VERSION:
        raise ContextGuardError("Context evidence uses an unsupported fingerprint version")
    if value.get("capturedSha256") != captured.fingerprint_sha256:
        raise ContextGuardError("Context evidence does not match the captured fingerprint")
    observations = value.get("observations")
    if not isinstance(observations, Mapping) or not observations:
        raise ContextGuardError("Context evidence must contain at least one observation")
    normalized: dict[str, Any] = {}
    for scenario, observation in observations.items():
        if scenario not in EVIDENCE_SCENARIOS or not isinstance(observation, Mapping):
            raise ContextGuardError("Context evidence contains an unsupported observation")
        fresh_sha256 = observation.get("freshSha256")
        verified_at = observation.get("verifiedAt")
        differences = observation.get("differences")
        fresh_snapshot = observation.get("freshSnapshot")
        if (
            not isinstance(fresh_sha256, str)
            or not isinstance(verified_at, str)
            or not verified_at
            or not isinstance(differences, list)
            or not all(isinstance(item, str) and item for item in differences)
            or not isinstance(fresh_snapshot, Mapping)
            or context_fingerprint(fresh_snapshot) != fresh_sha256
        ):
            raise ContextGuardError("Context evidence contains an invalid observation")
        matched = fresh_sha256 == captured.fingerprint_sha256 and not differences
        expected_status = "matched" if scenario == "matched" else "refused"
        if observation.get("status") != expected_status:
            raise ContextGuardError("Context evidence observation status is inconsistent")
        if (scenario == "matched") != matched:
            raise ContextGuardError("Context evidence observation result is inconsistent")
        normalized_observation = {
            "status": expected_status,
            "freshSha256": fresh_sha256,
            "verifiedAt": verified_at,
            "differences": list(differences),
            "freshSnapshot": fresh_snapshot,
        }
        if schema_version == EVIDENCE_SCHEMA_VERSION:
            freshness = validate_lineage_freshness_proof(
                observation.get("lineageFreshness")
            )
            if scenario in {"matched", "refused-drift"} and not freshness["matched"]:
                raise ContextGuardError(
                    "Context evidence scenario requires matching cache-bypassed lineage"
                )
            if scenario == "refused-drift" and "source ownership changed" not in differences:
                raise ContextGuardError(
                    "Owner-drift evidence must contain a source ownership change"
                )
            if scenario == "refused-lineage-drift" and not any(
                "lineage" in difference or "downstream" in difference
                for difference in differences
            ):
                raise ContextGuardError(
                    "Lineage-drift evidence must contain a downstream lineage change"
                )
            normalized_observation["lineageFreshness"] = freshness
        normalized[scenario] = normalized_observation
    return {
        "schemaVersion": schema_version,
        "campaignManifestSha256": campaign_manifest_sha256,
        "fingerprintVersion": FINGERPRINT_VERSION,
        "capturedSha256": captured.fingerprint_sha256,
        "observations": normalized,
    }


def write_context_verification_evidence(
    bundle_dir: str | Path,
    *,
    captured: CapturedContext,
    verification: ContextVerification,
    scenario: str,
    overwrite: bool = False,
) -> Path:
    """Atomically record one live verification observation beside a campaign."""
    if scenario not in EVIDENCE_SCENARIOS:
        raise ContextGuardError(f"Unsupported context evidence scenario: {scenario}")
    if verification.fresh_snapshot is None:
        raise ContextGuardError("Fresh context snapshot is required for evidence")
    if verification.captured_sha256 != captured.fingerprint_sha256:
        raise ContextGuardError("Fresh verification does not belong to this campaign")
    if verification.lineage_freshness is None:
        raise ContextGuardError(
            "Cache-bypassed lineage proof is required for context evidence"
        )
    if (scenario == "matched") != verification.matched:
        raise ContextGuardError(
            f"Context verification result does not match scenario {scenario!r}"
        )

    root = Path(bundle_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextGuardError(f"Could not read campaign manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ContextGuardError("Campaign manifest must contain a JSON object")
    manifest_captured = captured_context_from_manifest(manifest)
    if manifest_captured != captured:
        raise ContextGuardError("Captured context does not belong to this campaign manifest")
    manifest_sha256 = _manifest_sha256(manifest_path)
    path = root / "context-verification.json"
    observations: dict[str, Any] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContextGuardError(f"Could not read existing context evidence: {exc}") from exc
        if not isinstance(existing, Mapping):
            raise ContextGuardError("Context evidence must contain a JSON object")
        validated = validate_context_verification_evidence(
            existing,
            campaign_manifest_sha256=manifest_sha256,
            captured=captured,
        )
        if validated["schemaVersion"] != EVIDENCE_SCHEMA_VERSION:
            if not overwrite:
                raise ContextGuardError(
                    "Legacy context evidence must be replaced with --force"
                )
        else:
            observations.update(validated["observations"])
    if scenario in observations and not overwrite:
        raise ContextGuardError(
            f"Context evidence already contains scenario {scenario!r}; use --force to replace it"
        )
    observations[scenario] = {
        "status": "matched" if verification.matched else "refused",
        "freshSha256": verification.fresh_sha256,
        "verifiedAt": verification.verified_at,
        "differences": list(verification.differences),
        "freshSnapshot": verification.fresh_snapshot,
        "lineageFreshness": verification.lineage_freshness.to_dict(),
    }
    evidence = validate_context_verification_evidence(
        {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "campaignManifestSha256": manifest_sha256,
            "fingerprintVersion": FINGERPRINT_VERSION,
            "capturedSha256": captured.fingerprint_sha256,
            "observations": observations,
        },
        campaign_manifest_sha256=manifest_sha256,
        captured=captured,
    )
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=root, delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary and temporary.exists():
            temporary.unlink()
        raise ContextGuardError(f"Could not write context evidence: {exc}") from exc
    return path


def replace_context_verification_evidence(
    bundle_dir: str | Path,
    *,
    captured: CapturedContext,
    verifications: Mapping[str, ContextVerification],
) -> Path:
    """Atomically replace the sidecar with one complete verified scenario set."""
    if set(verifications) != set(EVIDENCE_SCENARIOS):
        raise ContextGuardError(
            "Complete context evidence requires matched, owner-drift, and "
            "lineage-drift observations"
        )
    root = Path(bundle_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextGuardError(f"Could not read campaign manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ContextGuardError("Campaign manifest must contain a JSON object")
    if captured_context_from_manifest(manifest) != captured:
        raise ContextGuardError("Captured context does not belong to this campaign manifest")

    observations: dict[str, Any] = {}
    for scenario in EVIDENCE_SCENARIOS:
        verification = verifications[scenario]
        if verification.fresh_snapshot is None or verification.lineage_freshness is None:
            raise ContextGuardError(
                f"Context evidence scenario {scenario!r} is incomplete"
            )
        if verification.captured_sha256 != captured.fingerprint_sha256:
            raise ContextGuardError(
                f"Context evidence scenario {scenario!r} belongs to another campaign"
            )
        if (scenario == "matched") != verification.matched:
            raise ContextGuardError(
                f"Context evidence scenario {scenario!r} has an inconsistent result"
            )
        observations[scenario] = {
            "status": "matched" if verification.matched else "refused",
            "freshSha256": verification.fresh_sha256,
            "verifiedAt": verification.verified_at,
            "differences": list(verification.differences),
            "freshSnapshot": verification.fresh_snapshot,
            "lineageFreshness": verification.lineage_freshness.to_dict(),
        }

    evidence = validate_context_verification_evidence(
        {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "campaignManifestSha256": _manifest_sha256(manifest_path),
            "fingerprintVersion": FINGERPRINT_VERSION,
            "capturedSha256": captured.fingerprint_sha256,
            "observations": observations,
        },
        campaign_manifest_sha256=_manifest_sha256(manifest_path),
        captured=captured,
    )
    path = root / "context-verification.json"
    payload = json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=root, delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, path)
    except OSError as exc:
        if temporary and temporary.exists():
            temporary.unlink()
        raise ContextGuardError(f"Could not replace context evidence: {exc}") from exc
    return path


def captured_context_from_manifest(manifest: Mapping[str, Any]) -> CapturedContext:
    datahub = manifest.get("dataHubContext") or {}
    if not isinstance(datahub, Mapping) or datahub.get("capture") != "live-mcp":
        raise ContextGuardError(
            "Campaign publication requires context captured from a live DataHub MCP read"
        )
    record = datahub.get("fingerprint") or {}
    if not isinstance(record, Mapping):
        raise ContextGuardError("Campaign is missing its DataHub context fingerprint")
    if record.get("version") != FINGERPRINT_VERSION or record.get("algorithm") != "sha256":
        raise ContextGuardError("Campaign uses an unsupported DataHub context fingerprint")
    snapshot = record.get("snapshot")
    fingerprint = record.get("sha256")
    if not isinstance(snapshot, Mapping) or not isinstance(fingerprint, str):
        raise ContextGuardError("Campaign has an incomplete DataHub context fingerprint")
    actual = context_fingerprint(snapshot)
    if actual != fingerprint:
        raise ContextGuardError("Campaign DataHub context fingerprint is corrupt")
    source = snapshot.get("source") or {}
    lineage = snapshot.get("lineage") or {}
    source_urn = source.get("urn") if isinstance(source, Mapping) else None
    source_column = source.get("column") if isinstance(source, Mapping) else None
    max_hops = lineage.get("maxHops") if isinstance(lineage, Mapping) else None
    if not isinstance(source_urn, str) or not source_urn:
        raise ContextGuardError("Campaign context is missing its source URN")
    if not isinstance(source_column, str) or not source_column:
        raise ContextGuardError("Campaign context is missing its source column")
    if not isinstance(max_hops, int) or max_hops < 1:
        raise ContextGuardError("Campaign context is missing a valid lineage hop limit")
    manifest_source = manifest.get("source") or {}
    rename = (
        manifest_source.get("rename") or {}
        if isinstance(manifest_source, Mapping)
        else {}
    )
    lineage_assets = manifest.get("lineageAssets")
    if (
        not isinstance(manifest_source, Mapping)
        or manifest_source.get("urn") != source_urn
        or not isinstance(rename, Mapping)
        or rename.get("oldColumn") != source_column
        or datahub.get("maxHops") != max_hops
        or not isinstance(lineage_assets, list)
    ):
        raise ContextGuardError(
            "Campaign manifest and captured DataHub context do not describe the same graph"
        )
    reconstructed = canonical_context_snapshot(
        source_urn=source_urn,
        source_column=source_column,
        max_hops=max_hops,
        schema_fields=source.get("schemaFields") or (),
        source_owners=manifest_source.get("owners") or (),
        downstream=lineage_assets,
    )
    if reconstructed != snapshot:
        raise ContextGuardError(
            "Campaign lineage or ownership evidence does not match its context fingerprint"
        )
    return CapturedContext(
        source_urn=source_urn,
        source_column=source_column,
        max_hops=max_hops,
        fingerprint_sha256=fingerprint,
        snapshot=snapshot,
    )


def _snapshot_differences(
    captured: Mapping[str, Any], fresh: Mapping[str, Any]
) -> tuple[str, ...]:
    differences: list[str] = []
    captured_source = captured.get("source") or {}
    fresh_source = fresh.get("source") or {}
    old_fields = set(captured_source.get("schemaFields") or ())
    new_fields = set(fresh_source.get("schemaFields") or ())
    if old_fields != new_fields:
        differences.append(
            "source schema changed"
            f" (added: {', '.join(sorted(new_fields - old_fields)) or 'none'};"
            f" removed: {', '.join(sorted(old_fields - new_fields)) or 'none'})"
        )
    if captured_source.get("owners") != fresh_source.get("owners"):
        differences.append("source ownership changed")

    old_assets = {
        item.get("urn"): item
        for item in (captured.get("lineage") or {}).get("downstream", ())
        if isinstance(item, Mapping) and item.get("urn")
    }
    new_assets = {
        item.get("urn"): item
        for item in (fresh.get("lineage") or {}).get("downstream", ())
        if isinstance(item, Mapping) and item.get("urn")
    }
    added = sorted(set(new_assets) - set(old_assets))
    removed = sorted(set(old_assets) - set(new_assets))
    if added:
        differences.append(f"downstream assets added: {', '.join(added)}")
    if removed:
        differences.append(f"downstream assets removed: {', '.join(removed)}")
    for urn in sorted(set(old_assets) & set(new_assets)):
        if old_assets[urn] != new_assets[urn]:
            differences.append(f"downstream metadata changed: {urn}")
    if not differences and captured != fresh:
        differences.append("captured DataHub context changed")
    return tuple(differences)


def _error_detail(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if nested:
        return _error_detail(nested[0])
    return str(exc) or type(exc).__name__


async def verify_fresh_context(
    captured: CapturedContext,
    *,
    reader: Callable[[str, str, int], Awaitable[Any]] | None = None,
    lineage_reader: Callable[[str, str, int], Awaitable[Any]] | None = None,
) -> ContextVerification:
    if reader is None:
        from .main import read_asset_context

        reader = read_asset_context
    if lineage_reader is None:
        if getattr(reader, "__module__", None) != "lineage_agent.main":
            raise ContextGuardError(
                "An injected MCP context reader requires an injected cache-bypassed "
                "lineage reader"
            )
        from .lineage_freshness import read_cache_bypassed_lineage

        lineage_reader = read_cache_bypassed_lineage
    try:
        context = await reader(
            captured.source_urn,
            captured.source_column,
            captured.max_hops,
        )
    except Exception as exc:
        raise ContextGuardError(
            f"Repeated DataHub MCP context read failed: {_error_detail(exc)}"
        ) from exc
    try:
        uncached_lineage = await lineage_reader(
            captured.source_urn,
            captured.source_column,
            captured.max_hops,
        )
    except Exception as exc:
        raise ContextGuardError(
            f"Cache-bypassed DataHub lineage read failed: {_error_detail(exc)}"
        ) from exc
    lineage_freshness = _lineage_freshness_proof(
        context.downstream,
        uncached_lineage,
    )
    fresh = canonical_context_snapshot(
        source_urn=captured.source_urn,
        source_column=captured.source_column,
        max_hops=captured.max_hops,
        schema_fields=context.schema_fields,
        source_owners=context.source_owners,
        downstream=context.downstream,
    )
    fresh_sha256 = context_fingerprint(fresh)
    differences = (
        *_snapshot_differences(captured.snapshot, fresh),
        *lineage_freshness.differences,
    )
    return ContextVerification(
        captured_sha256=captured.fingerprint_sha256,
        fresh_sha256=fresh_sha256,
        differences=differences,
        verified_at=datetime.now(timezone.utc).isoformat(),
        fresh_snapshot=fresh,
        lineage_freshness=lineage_freshness,
    )


def require_fresh_context(
    captured: CapturedContext,
    verification: ContextVerification | None,
) -> None:
    if verification is None:
        raise ContextGuardError("A repeated DataHub context verification is required")
    if verification.captured_sha256 != captured.fingerprint_sha256:
        raise ContextGuardError(
            "Repeated DataHub verification does not belong to this campaign context"
        )
    freshness = verification.lineage_freshness
    if freshness is None or freshness.method != CACHE_BYPASS_METHOD or not freshness.skip_cache:
        raise ContextGuardError(
            "DataHub context verification is missing a cache-bypassed lineage read"
        )
    if not freshness.matched:
        detail = "; ".join(freshness.differences) or "lineage signatures differ"
        raise ContextGuardError(
            f"DataHub lineage freshness check failed: {detail}. "
            "Regenerate and review the campaign before continuing"
        )
    if not verification.matched:
        detail = "; ".join(verification.differences) or "fingerprint changed"
        raise ContextGuardError(
            f"DataHub context drifted after campaign generation: {detail}. "
            "Regenerate and review the campaign before continuing"
        )


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare captured context with repeated MCP reads and cache-bypassed lineage."
        )
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--record-evidence", action="store_true")
    parser.add_argument("--scenario", choices=EVIDENCE_SCENARIOS)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.record_evidence != bool(args.scenario):
        parser.error("--record-evidence and --scenario must be supplied together")
    if args.force and not args.record_evidence:
        parser.error("--force requires --record-evidence")
    return args


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _arguments(argv)
    evidence_path = None
    try:
        manifest_path = Path(args.bundle).expanduser().resolve() / "manifest.json"
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ContextGuardError("Campaign manifest must contain a JSON object")
        captured = captured_context_from_manifest(value)
        verification = asyncio.run(verify_fresh_context(captured))
        evidence_path = None
        if args.record_evidence:
            evidence_path = write_context_verification_evidence(
                manifest_path.parent,
                captured=captured,
                verification=verification,
                scenario=args.scenario,
                overwrite=args.force,
            )
        require_fresh_context(captured, verification)
    except (OSError, json.JSONDecodeError, ContextGuardError) as exc:
        print(f"RippleProof context verification failed: {exc}", file=sys.stderr)
        if evidence_path:
            print(f"  Evidence: {evidence_path}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("DataHub context matches the captured campaign")
    print("  Lineage cache: bypassed and matched")
    print(f"  SHA-256: {verification.fresh_sha256}")
    print(f"  Verified at: {verification.verified_at}")
    if evidence_path:
        print(f"  Evidence: {evidence_path}")


if __name__ == "__main__":
    main()
