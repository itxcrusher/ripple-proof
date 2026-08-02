"""Audit a captured change campaign without network or service access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .replay import load_campaign


AUDIT_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class AuditCheck:
    check_id: str
    label: str
    status: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.check_id,
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CampaignAudit:
    bundle_name: str
    campaign_manifest_sha256: str | None
    status: str
    summary: str
    metrics: Mapping[str, int]
    checks: tuple[AuditCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "evidenceMode": "captured-offline",
            "bundleName": self.bundle_name,
            "campaignManifestSha256": self.campaign_manifest_sha256,
            "status": self.status,
            "summary": self.summary,
            "metrics": dict(self.metrics),
            "checks": [check.to_dict() for check in self.checks],
        }


def _check(check_id: str, label: str, status: str, detail: str) -> AuditCheck:
    return AuditCheck(check_id=check_id, label=label, status=status, detail=detail)


def _bundle_file(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError("artifact path is missing")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"artifact escapes the bundle: {relative}") from None
    if not candidate.is_file():
        raise ValueError(f"artifact is missing: {relative}")
    return candidate


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _overall_status(checks: Sequence[AuditCheck]) -> str:
    if any(check.status == "failed" for check in checks):
        return "failed"
    if any(check.status == "incomplete" for check in checks):
        return "incomplete"
    return "passed"


def _artifact_check(root: Path, manifest: Mapping[str, Any]) -> AuditCheck:
    entries = list(manifest.get("downstreamPatches") or ()) + list(
        manifest.get("contractPatches") or ()
    )
    if not entries:
        return _check(
            "artifact-integrity",
            "Generated artifact integrity",
            "failed",
            "The campaign contains no generated SQL or dbt contract patches.",
        )
    failures: list[str] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            failures.append("invalid artifact entry")
            continue
        try:
            diff_path = _bundle_file(root, entry.get("diffPath"))
            proposed_path = _bundle_file(root, entry.get("proposedPath"))
        except ValueError as exc:
            failures.append(str(exc))
            continue
        if not diff_path.read_text(encoding="utf-8").strip():
            failures.append(f"empty diff: {entry.get('diffPath')}")
        expected = entry.get("proposedSha256")
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            failures.append(f"missing proposed SHA-256: {entry.get('proposedPath')}")
        elif _file_sha256(proposed_path) != expected:
            failures.append(f"proposed SHA-256 mismatch: {entry.get('proposedPath')}")
        source_hash = entry.get("sourceSha256")
        if not isinstance(source_hash, str) or not SHA256_RE.fullmatch(source_hash):
            failures.append(f"missing source SHA-256: {entry.get('sourcePath')}")
    if failures:
        return _check(
            "artifact-integrity",
            "Generated artifact integrity",
            "failed",
            "; ".join(failures[:4]),
        )
    repositories = {entry.get("repository") for entry in entries}
    return _check(
        "artifact-integrity",
        "Generated artifact integrity",
        "passed",
        f"{len(entries)} generated file(s) across {len(repositories)} repository mapping(s) have non-empty diffs and matching proposed hashes.",
    )


def _datahub_check(manifest: Mapping[str, Any]) -> AuditCheck:
    context = manifest.get("dataHubContext") or {}
    reads = context.get("reads") or () if isinstance(context, Mapping) else ()
    tools = {
        str(read.get("tool"))
        for read in reads
        if isinstance(read, Mapping) and read.get("tool")
    }
    required = {
        "list_schema_fields",
        "get_lineage(column)",
        "get_lineage(asset)",
        "get_entities(ownership)",
    }
    fingerprint = context.get("fingerprint") or {} if isinstance(context, Mapping) else {}
    fingerprint_sha = fingerprint.get("sha256") if isinstance(fingerprint, Mapping) else None
    missing = sorted(required - tools)
    if context.get("capture") != "live-mcp" or missing or not isinstance(
        fingerprint_sha, str
    ) or not SHA256_RE.fullmatch(fingerprint_sha):
        details = []
        if context.get("capture") != "live-mcp":
            details.append("capture is not labeled live-mcp")
        if missing:
            details.append(f"missing read summaries: {', '.join(missing)}")
        if not isinstance(fingerprint_sha, str) or not SHA256_RE.fullmatch(fingerprint_sha):
            details.append("canonical context fingerprint is missing")
        return _check(
            "datahub-capture",
            "DataHub MCP capture",
            "failed",
            "; ".join(details),
        )
    return _check(
        "datahub-capture",
        "DataHub MCP capture",
        "passed",
        f"Schema, column lineage, asset lineage, and ownership reads are summarized and bound to SHA-256 {fingerprint_sha}.",
    )


def _lineage_check(manifest: Mapping[str, Any]) -> AuditCheck:
    lineage = {
        item.get("urn")
        for item in manifest.get("lineageAssets") or ()
        if isinstance(item, Mapping) and item.get("urn")
    }
    dispositions: dict[str, str] = {}
    duplicates: set[str] = set()
    for field, label in (
        ("downstreamPatches", "patched"),
        ("protectedByContract", "protected"),
        ("manualReview", "manual-review"),
    ):
        for item in manifest.get(field) or ():
            if not isinstance(item, Mapping):
                continue
            urn = item.get("assetUrn")
            if not urn:
                continue
            if urn in dispositions:
                duplicates.add(str(urn))
            dispositions[str(urn)] = label
    missing = sorted(str(urn) for urn in lineage - set(dispositions))
    unexpected = sorted(str(urn) for urn in set(dispositions) - lineage)
    if not lineage or missing or duplicates or unexpected:
        details = []
        if not lineage:
            details.append("no downstream lineage assets were recorded")
        if missing:
            details.append(f"no disposition: {', '.join(missing)}")
        if duplicates:
            details.append(f"multiple dispositions: {', '.join(sorted(duplicates))}")
        if unexpected:
            details.append(f"disposition outside lineage: {', '.join(unexpected)}")
        return _check(
            "lineage-accounting",
            "Complete lineage accounting",
            "failed",
            "; ".join(details),
        )
    manual = sum(1 for value in dispositions.values() if value == "manual-review")
    return _check(
        "lineage-accounting",
        "Complete lineage accounting",
        "passed",
        f"All {len(lineage)} reachable asset(s) are patched, protected by a stable contract, or explicitly routed for manual review ({manual} manual).",
    )


def _routing_check(manifest: Mapping[str, Any]) -> AuditCheck:
    source = manifest.get("source") or {}
    expected = {
        item.get("urn")
        for item in manifest.get("lineageAssets") or ()
        if isinstance(item, Mapping) and item.get("urn")
    }
    if isinstance(source, Mapping) and source.get("urn"):
        expected.add(source.get("urn"))
    assignments = manifest.get("reviewAssignments") or ()
    assigned = {
        item.get("assetUrn")
        for item in assignments
        if isinstance(item, Mapping)
        and item.get("routingStatus") == "assigned"
        and item.get("reviewers")
    }
    missing = sorted(str(urn) for urn in expected - assigned)
    if not expected or missing:
        return _check(
            "review-routing",
            "Owner-based review routing",
            "failed",
            "No review scope was recorded."
            if not expected
            else f"Missing an assigned DataHub owner route for: {', '.join(missing)}.",
        )
    return _check(
        "review-routing",
        "Owner-based review routing",
        "passed",
        f"DataHub ownership assigns a reviewer route to all {len(expected)} source and downstream asset(s).",
    )


def _validation_check(manifest: Mapping[str, Any]) -> AuditCheck:
    validations = manifest.get("validations") or ()
    failed = [
        str(item.get("id"))
        for item in validations
        if isinstance(item, Mapping) and item.get("status") == "failed"
    ]
    if manifest.get("readyForReview") is not True or failed:
        detail = "Campaign is not marked ready for review."
        if failed:
            detail += f" Failed gates: {', '.join(failed)}."
        return _check(
            "review-readiness",
            "Deterministic review readiness",
            "failed",
            detail,
        )
    warnings = sum(
        1
        for item in validations
        if isinstance(item, Mapping) and item.get("status") == "warning"
    )
    return _check(
        "review-readiness",
        "Deterministic review readiness",
        "passed",
        f"All blocking gates passed; {warnings} non-blocking warning(s) remain visible for human review.",
    )


def _dbt_check(manifest: Mapping[str, Any]) -> AuditCheck:
    validation = manifest.get("dbtValidation")
    if not isinstance(validation, Mapping) or validation.get("status") == "not-requested":
        return _check(
            "dbt-execution",
            "Executable dbt validation",
            "incomplete",
            "No captured dbt execution evidence is present.",
        )
    projects = validation.get("projects") or ()
    failing = []
    for project in projects:
        if not isinstance(project, Mapping):
            failing.append("invalid project")
            continue
        build_steps = [
            step
            for step in project.get("steps") or ()
            if isinstance(step, Mapping) and step.get("name") == "dbt-build"
        ]
        if (
            project.get("status") != "passed"
            or not build_steps
            or any(step.get("status") != "passed" for step in build_steps)
        ):
            failing.append(str(project.get("repository") or "unknown project"))
    if validation.get("status") != "passed" or not projects or failing:
        return _check(
            "dbt-execution",
            "Executable dbt validation",
            "failed",
            "dbt build evidence failed or is missing for: "
            + (", ".join(failing) if failing else "all projects"),
        )
    return _check(
        "dbt-execution",
        "Executable dbt validation",
        "passed",
        f"dbt {validation.get('dbtVersion')} built and tested {len(projects)} isolated validation root(s) with {validation.get('adapter')}.",
    )


def _postgres_check(campaign: Mapping[str, Any]) -> AuditCheck:
    evidence = campaign.get("postgresValidationEvidence")
    if not isinstance(evidence, Mapping):
        return _check(
            "postgres-rollback",
            "Disposable PostgreSQL rollback",
            "incomplete",
            "No manifest-bound PostgreSQL execution evidence is present.",
        )
    validation = evidence.get("validation") or {}
    old_column = validation.get("oldColumn")
    new_column = validation.get("newColumn")
    passed = (
        validation.get("status") == "passed"
        and validation.get("beforeColumns") == [old_column]
        and validation.get("afterColumns") == [new_column]
        and validation.get("rollbackColumns") == [old_column]
    )
    if not passed:
        return _check(
            "postgres-rollback",
            "Disposable PostgreSQL rollback",
            "failed",
            "Captured PostgreSQL evidence does not prove the expected before, renamed, and rolled-back schema states.",
        )
    return _check(
        "postgres-rollback",
        "Disposable PostgreSQL rollback",
        "passed",
        f"PostgreSQL {validation.get('engineVersion')} observed {old_column} -> {new_column} -> {old_column} inside an isolated rolled-back database.",
    )


def _freshness_check(campaign: Mapping[str, Any]) -> AuditCheck:
    evidence = campaign.get("contextVerification")
    if not isinstance(evidence, Mapping):
        return _check(
            "context-behavior",
            "Cache-bypassed context and drift refusal",
            "incomplete",
            "No captured mutation-time context observation is present.",
        )
    observations = evidence.get("observations") or {}
    matched = observations.get("matched") or {} if isinstance(observations, Mapping) else {}
    refused = observations.get("refused-drift") or {} if isinstance(observations, Mapping) else {}
    refused_lineage = (
        observations.get("refused-lineage-drift") or {}
        if isinstance(observations, Mapping)
        else {}
    )
    matched_lineage = matched.get("lineageFreshness") or {}
    refused_owner_lineage = refused.get("lineageFreshness") or {}
    refused_lineage_proof = refused_lineage.get("lineageFreshness") or {}
    if (
        evidence.get("schemaVersion") != 2
        or matched.get("status") != "matched"
        or matched.get("differences") != []
        or matched_lineage.get("skipCache") is not True
        or matched_lineage.get("matched") is not True
        or refused.get("status") != "refused"
        or not refused.get("differences")
        or refused_owner_lineage.get("skipCache") is not True
        or refused_owner_lineage.get("matched") is not True
        or refused_lineage.get("status") != "refused"
        or not refused_lineage.get("differences")
        or refused_lineage_proof.get("skipCache") is not True
        or refused_lineage_proof.get("matched") is not False
    ):
        return _check(
            "context-behavior",
            "Cache-bypassed context and drift refusal",
            "failed",
            "The sidecar must prove a cache-bypassed match plus owner-drift and removed-lineage refusals.",
        )
    return _check(
        "context-behavior",
        "Cache-bypassed context and drift refusal",
        "passed",
        "Unchanged and owner-drift reads matched cache-bypassed lineage; a removed edge exposed stale MCP lineage and was refused.",
    )


def _publication_check(campaign: Mapping[str, Any]) -> AuditCheck:
    publication = campaign.get("publication")
    if not isinstance(publication, Mapping):
        return _check(
            "github-delivery",
            "Review-only GitHub delivery",
            "incomplete",
            "No manifest-bound GitHub publication evidence is present.",
        )
    repositories = publication.get("repositories") or ()
    invalid: list[str] = []
    for item in repositories:
        if not isinstance(item, Mapping):
            invalid.append("invalid repository entry")
            continue
        if (
            item.get("autoMergeEnabled") is not False
            or not isinstance(item.get("pullRequest"), Mapping)
            or item["pullRequest"].get("url") is None
            or not isinstance(item.get("checks"), Mapping)
            or item["checks"].get("status") != "passed"
        ):
            invalid.append(str(item.get("repository") or "unknown repository"))
    passed = (
        publication.get("status") == "passed"
        and publication.get("mergePolicy") == "manual-only"
        and publication.get("autoMergeEnabled") is False
        and bool(repositories)
        and not invalid
    )
    if not passed:
        return _check(
            "github-delivery",
            "Review-only GitHub delivery",
            "failed",
            "Publication is not fully check-passing and manual-only"
            + (f" for: {', '.join(invalid)}" if invalid else "."),
        )
    return _check(
        "github-delivery",
        "Review-only GitHub delivery",
        "passed",
        f"{len(repositories)} pull request(s) were recorded with required checks passing, auto-merge disabled, and merge policy manual-only.",
    )


def _decision_check(campaign: Mapping[str, Any]) -> AuditCheck:
    decision = campaign.get("decision")
    if not isinstance(decision, Mapping):
        return _check(
            "datahub-decision",
            "DataHub decision readback",
            "incomplete",
            "No manifest-bound structured DataHub decision is present.",
        )
    assets = decision.get("assets")
    verified = decision.get("verifiedAssets")
    passed = (
        decision.get("decision") == "approved-for-review"
        and decision.get("mergePolicy") == "manual-only"
        and decision.get("freshSessionReadback") is True
        and isinstance(assets, list)
        and assets == verified
        and bool(assets)
    )
    if not passed:
        return _check(
            "datahub-decision",
            "DataHub decision readback",
            "failed",
            "The structured decision was not read back for every covered asset in a fresh client session.",
        )
    return _check(
        "datahub-decision",
        "DataHub decision readback",
        "passed",
        f"The approved-for-review decision was read back for all {len(assets)} covered asset(s); merge remains manual-only.",
    )


def _safety_check(campaign: Mapping[str, Any]) -> AuditCheck:
    manifest = campaign["manifest"]
    safety = manifest.get("safety") or {}
    publication = campaign.get("publication") or {}
    passed = (
        safety.get("sqlExecuted") is False
        and safety.get("targetRepositoriesModified") is False
        and safety.get("humanReviewRequired") is True
        and (
            not publication
            or (
                publication.get("mergePolicy") == "manual-only"
                and publication.get("autoMergeEnabled") is False
            )
        )
    )
    if not passed:
        return _check(
            "safety-boundary",
            "Human-controlled safety boundary",
            "failed",
            "The captured campaign does not prove non-execution, repository immutability, human review, and manual-only merge controls.",
        )
    return _check(
        "safety-boundary",
        "Human-controlled safety boundary",
        "passed",
        "The source migration was not executed, source repositories were not modified, human review is required, and delivery cannot auto-merge.",
    )


def audit_campaign(bundle_dir: str | Path) -> CampaignAudit:
    """Return a deterministic offline audit of one captured bundle."""
    root = Path(bundle_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    manifest_sha = _file_sha256(manifest_path) if manifest_path.is_file() else None
    try:
        campaign = load_campaign(root)
    except (OSError, ValueError) as exc:
        check = _check(
            "bundle-load",
            "Bundle structure and evidence binding",
            "failed",
            str(exc),
        )
        return CampaignAudit(
            bundle_name=root.name,
            campaign_manifest_sha256=manifest_sha,
            status="failed",
            summary="0 of 1 checks passed; the campaign bundle could not be trusted.",
            metrics={},
            checks=(check,),
        )

    manifest = campaign["manifest"]
    checks = (
        _artifact_check(root, manifest),
        _datahub_check(manifest),
        _lineage_check(manifest),
        _routing_check(manifest),
        _validation_check(manifest),
        _dbt_check(manifest),
        _postgres_check(campaign),
        _freshness_check(campaign),
        _publication_check(campaign),
        _decision_check(campaign),
        _safety_check(campaign),
    )
    status = _overall_status(checks)
    passed = sum(check.status == "passed" for check in checks)
    incomplete = sum(check.status == "incomplete" for check in checks)
    failed = sum(check.status == "failed" for check in checks)
    summary = f"{passed} of {len(checks)} checks passed"
    if incomplete:
        summary += f"; {incomplete} evidence area(s) incomplete"
    if failed:
        summary += f"; {failed} check(s) failed"
    summary += ". This is an offline verification of captured evidence, not a fresh service check."
    metrics = {
        "lineageAssets": len(manifest.get("lineageAssets") or ()),
        "targetRepositories": len(manifest.get("targetRepositories") or ()),
        "generatedFiles": len(manifest.get("downstreamPatches") or ())
        + len(manifest.get("contractPatches") or ()),
        "reviewRoutes": len(manifest.get("reviewAssignments") or ()),
        "publishedPullRequests": len(
            (campaign.get("publication") or {}).get("repositories") or ()
        ),
        "decisionAssets": len((campaign.get("decision") or {}).get("assets") or ()),
    }
    return CampaignAudit(
        bundle_name=root.name,
        campaign_manifest_sha256=manifest_sha,
        status=status,
        summary=summary,
        metrics=metrics,
        checks=checks,
    )


def render_audit_text(audit: CampaignAudit) -> str:
    status_label = audit.status.upper()
    lines = [
        f"CAMPAIGN AUDIT: {status_label}",
        f"Bundle: {audit.bundle_name}",
        f"Manifest SHA-256: {audit.campaign_manifest_sha256 or 'unavailable'}",
        audit.summary,
        "",
    ]
    symbols = {"passed": "PASS", "incomplete": "WAIT", "failed": "FAIL"}
    for check in audit.checks:
        lines.append(f"[{symbols[check.status]}] {check.label}")
        lines.append(f"       {check.detail}")
    return "\n".join(lines)


def write_audit(
    audit: CampaignAudit,
    bundle_dir: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    root = Path(bundle_dir).expanduser().resolve()
    output = root / "verification.json"
    if output.exists() and not overwrite:
        raise ValueError("Campaign verification already exists; use --force to replace it")
    payload = json.dumps(audit.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=root, delete=False
        ) as handle:
            handle.write(payload)
            temporary = Path(handle.name)
        os.replace(temporary, output)
    except OSError as exc:
        if temporary and temporary.exists():
            temporary.unlink()
        raise ValueError(f"Could not write campaign verification: {exc}") from exc
    return output


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a captured change campaign without network or service access."
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--json", action="store_true", help="Print the audit as JSON.")
    parser.add_argument(
        "--record",
        action="store_true",
        help="Write manifest-bound verification.json into the bundle.",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.force and not args.record:
        parser.error("--force requires --record")
    return args


def main(argv: list[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _arguments(argv)
    audit = audit_campaign(args.bundle)
    print(
        json.dumps(audit.to_dict(), ensure_ascii=False, indent=2)
        if args.json
        else render_audit_text(audit)
    )
    if args.record:
        try:
            output = write_audit(audit, args.bundle, overwrite=args.force)
        except ValueError as exc:
            print(f"Could not record campaign audit: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        print(f"Evidence: {output}")
    if audit.status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
