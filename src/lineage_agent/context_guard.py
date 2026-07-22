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


FINGERPRINT_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_SCENARIOS = ("matched", "refused-drift")


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
class ContextVerification:
    captured_sha256: str
    fresh_sha256: str
    differences: tuple[str, ...]
    verified_at: str
    fresh_snapshot: Mapping[str, Any] | None = None

    @property
    def matched(self) -> bool:
        return self.captured_sha256 == self.fresh_sha256 and not self.differences

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "matched" if self.matched else "drifted",
            "capturedSha256": self.captured_sha256,
            "freshSha256": self.fresh_sha256,
            "verifiedAt": self.verified_at,
            "differences": list(self.differences),
        }


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


def _manifest_sha256(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def validate_context_verification_evidence(
    value: Mapping[str, Any],
    *,
    campaign_manifest_sha256: str,
    captured: CapturedContext,
) -> dict[str, Any]:
    """Validate a manifest-bound context verification sidecar."""
    if value.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION:
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
        normalized[scenario] = {
            "status": expected_status,
            "freshSha256": fresh_sha256,
            "verifiedAt": verified_at,
            "differences": list(differences),
            "freshSnapshot": fresh_snapshot,
        }
    return {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
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
) -> ContextVerification:
    if reader is None:
        from .main import read_asset_context

        reader = read_asset_context
    try:
        context = await reader(
            captured.source_urn,
            captured.source_column,
            captured.max_hops,
        )
    except Exception as exc:
        raise ContextGuardError(
            f"Fresh DataHub MCP context read failed: {_error_detail(exc)}"
        ) from exc
    fresh = canonical_context_snapshot(
        source_urn=captured.source_urn,
        source_column=captured.source_column,
        max_hops=captured.max_hops,
        schema_fields=context.schema_fields,
        source_owners=context.source_owners,
        downstream=context.downstream,
    )
    fresh_sha256 = context_fingerprint(fresh)
    return ContextVerification(
        captured_sha256=captured.fingerprint_sha256,
        fresh_sha256=fresh_sha256,
        differences=_snapshot_differences(captured.snapshot, fresh),
        verified_at=datetime.now(timezone.utc).isoformat(),
        fresh_snapshot=fresh,
    )


def require_fresh_context(
    captured: CapturedContext,
    verification: ContextVerification | None,
) -> None:
    if verification is None:
        raise ContextGuardError("A fresh DataHub context verification is required")
    if verification.captured_sha256 != captured.fingerprint_sha256:
        raise ContextGuardError(
            "Fresh DataHub verification does not belong to this campaign context"
        )
    if not verification.matched:
        detail = "; ".join(verification.differences) or "fingerprint changed"
        raise ContextGuardError(
            f"DataHub context drifted after campaign generation: {detail}. "
            "Regenerate and review the campaign before continuing"
        )


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare a campaign's captured DataHub context with fresh MCP reads."
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
        print(f"BlastRadius context verification failed: {exc}", file=sys.stderr)
        if evidence_path:
            print(f"  Evidence: {evidence_path}", file=sys.stderr)
        raise SystemExit(2) from exc
    print("DataHub context matches the captured campaign")
    print(f"  SHA-256: {verification.fresh_sha256}")
    print(f"  Verified at: {verification.verified_at}")
    if evidence_path:
        print(f"  Evidence: {evidence_path}")


if __name__ == "__main__":
    main()
