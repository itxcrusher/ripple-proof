"""Render self-contained campaign review pages from RippleProof bundles."""

from __future__ import annotations

from . import design

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .context_guard import (
    ContextGuardError,
    captured_context_from_manifest,
    validate_context_verification_evidence,
)
from .postgres_validation import validate_postgres_evidence


SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_SHA_RE = re.compile(r"^[a-f0-9]{40}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PROPERTY_PREFIX = "urn:li:structuredProperty:io.github.itxcrusher.ripple_proof."
AUDIT_CHECK_IDS = {
    "artifact-integrity",
    "datahub-capture",
    "lineage-accounting",
    "review-routing",
    "review-readiness",
    "dbt-execution",
    "postgres-rollback",
    "context-behavior",
    "github-delivery",
    "datahub-decision",
    "safety-boundary",
}


def _patch_values(
    entry: Mapping[str, Any],
    plural: str,
    singular: str,
    label: str,
) -> tuple[str, ...]:
    values = entry.get(plural)
    fallback = entry.get(singular)
    if values is None:
        if not isinstance(fallback, str) or not fallback:
            raise ValueError(f"Campaign patch does not identify its {label}")
        return (fallback,)
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and value for value in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError(f"Campaign patch contains invalid {plural}")
    if fallback is not None and (
        not isinstance(fallback, str) or fallback not in values
    ):
        raise ValueError(f"Campaign patch {singular} conflicts with {plural}")
    return tuple(values)


def patch_asset_urns(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every asset covered by a coalesced patch, with legacy fallback."""
    return _patch_values(entry, "assetUrns", "assetUrn", "asset URN")


def patch_asset_names(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every asset name covered by a coalesced patch, with legacy fallback."""
    return _patch_values(entry, "assetNames", "assetName", "asset name")


def expected_decision_assets(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    source = manifest.get("source")
    if not isinstance(source, Mapping) or not isinstance(source.get("urn"), str):
        raise ValueError("Campaign manifest does not identify its source asset")
    result = [source["urn"]]
    lineage = manifest.get("lineageAssets")
    if not isinstance(lineage, list):
        raise ValueError("Campaign manifest lineageAssets must contain a list")
    for asset in lineage:
        if not isinstance(asset, Mapping) or not isinstance(asset.get("urn"), str):
            raise ValueError("Campaign manifest contains an invalid lineage asset")
        urn = asset["urn"]
        if urn in result:
            raise ValueError(f"Campaign manifest repeats covered asset: {urn}")
        result.append(urn)
    return tuple(result)


def _safe_repository_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Publication proof contains an invalid repository file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Publication proof contains an unsafe repository file path")
    return path.as_posix()


def _expected_publication_scope(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    scope: dict[str, dict[str, str]] = {}
    for key in ("downstreamPatches", "contractPatches"):
        entries = manifest.get(key)
        if not isinstance(entries, list):
            raise ValueError(f"Campaign manifest field {key!r} must contain a list")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ValueError(f"Campaign manifest contains an invalid {key} entry")
            label = entry.get("repository")
            path = _safe_repository_path(entry.get("sourcePath"))
            digest = entry.get("proposedSha256")
            if not isinstance(label, str) or not label:
                raise ValueError("Campaign patch does not identify its repository")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise ValueError(f"Campaign patch does not bind proposed content: {path}")
            repository_files = scope.setdefault(label, {})
            if path in repository_files:
                raise ValueError(f"Campaign contains duplicate publication path: {label}/{path}")
            repository_files[path] = digest
    if not scope:
        raise ValueError("Campaign manifest contains no publishable repository scope")
    return scope


def validate_publication_evidence(
    manifest: Mapping[str, Any],
    publication: Mapping[str, Any],
    *,
    campaign_manifest_sha256: str,
) -> dict[str, Any]:
    """Bind GitHub proof to every manifest repository, file, commit, and check."""
    if publication.get("schemaVersion") != 1:
        raise ValueError("Publication evidence uses an unsupported schema version")
    if publication.get("campaignManifestSha256") != campaign_manifest_sha256:
        raise ValueError("Publication evidence does not match the campaign manifest")
    if (
        publication.get("status") != "passed"
        or publication.get("mergePolicy") != "manual-only"
        or publication.get("autoMergeEnabled") is not False
    ):
        raise ValueError("Publication evidence is not passed and manual-only")
    required_checks = publication.get("requiredChecks")
    if (
        not isinstance(required_checks, list)
        or not required_checks
        or not all(isinstance(name, str) and name for name in required_checks)
        or len(set(required_checks)) != len(required_checks)
    ):
        raise ValueError("Publication evidence does not identify its required checks")

    expected_scope = _expected_publication_scope(manifest)
    repositories = publication.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("Publication evidence contains no repositories")
    normalized_repositories: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    seen_remotes: set[str] = set()
    for item in repositories:
        if not isinstance(item, Mapping):
            raise ValueError("Publication evidence contains an invalid repository entry")
        label = item.get("label")
        remote = item.get("repository")
        commit_sha = item.get("commitSha")
        if not isinstance(label, str) or label not in expected_scope:
            raise ValueError(f"Publication proof has an unexpected repository label: {label}")
        if label in seen_labels:
            raise ValueError(f"Publication proof repeats repository label: {label}")
        if not isinstance(remote, str) or not REPOSITORY_RE.fullmatch(remote):
            raise ValueError(f"Publication proof has an invalid GitHub repository: {remote}")
        if remote in seen_remotes:
            raise ValueError(f"Publication proof reuses GitHub repository: {remote}")
        if not isinstance(commit_sha, str) or not COMMIT_SHA_RE.fullmatch(commit_sha):
            raise ValueError(f"Publication proof has an invalid commit SHA: {label}")
        seen_labels.add(label)
        seen_remotes.add(remote)

        base_branch = item.get("baseBranch")
        head_branch = item.get("headBranch")
        pull_request = item.get("pullRequest")
        if (
            item.get("autoMergeEnabled") is not False
            or not isinstance(base_branch, str)
            or not base_branch
            or not isinstance(head_branch, str)
            or not head_branch
            or not isinstance(pull_request, Mapping)
            or pull_request.get("state") != "OPEN"
            or pull_request.get("autoMergeRequest") is not None
            or pull_request.get("baseRefName") != base_branch
            or pull_request.get("headRefName") != head_branch
            or not isinstance(pull_request.get("number"), int)
            or pull_request["number"] < 1
        ):
            raise ValueError(f"Publication proof has an invalid pull request: {label}")
        expected_url = f"https://github.com/{remote}/pull/{pull_request['number']}"
        if pull_request.get("url") != expected_url:
            raise ValueError(f"Publication proof pull request URL does not match: {label}")

        checks = item.get("checks")
        if not isinstance(checks, Mapping):
            raise ValueError(f"Publication proof has no check evidence: {label}")
        recorded_required = checks.get("requiredChecks")
        conclusions = checks.get("checks")
        if (
            checks.get("status") != "passed"
            or checks.get("timedOut") is not False
            or checks.get("waitExitCode") != 0
            or not isinstance(recorded_required, list)
            or set(recorded_required) != set(required_checks)
            or not isinstance(conclusions, list)
        ):
            raise ValueError(f"Publication proof required-check summary is invalid: {label}")
        passing_names = {
            check.get("name")
            for check in conclusions
            if isinstance(check, Mapping)
            and check.get("bucket") == "pass"
            and str(check.get("state") or "").upper() == "SUCCESS"
        }
        missing_checks = sorted(set(required_checks) - passing_names)
        if missing_checks:
            raise ValueError(
                f"Publication proof lacks passing required check conclusions for {label}: "
                + ", ".join(missing_checks)
            )

        expected_files = expected_scope[label]
        files = item.get("files")
        if files is None:
            normalized_files = [
                {"path": path, "sha256": digest}
                for path, digest in sorted(expected_files.items())
            ]
        else:
            if not isinstance(files, list) or not files:
                raise ValueError(f"Publication proof contains no verified files: {label}")
            recorded_files: dict[str, str] = {}
            for file in files:
                if not isinstance(file, Mapping):
                    raise ValueError(f"Publication proof contains an invalid file: {label}")
                path = _safe_repository_path(file.get("path"))
                digest = file.get("sha256")
                if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                    raise ValueError(f"Publication proof has an invalid file hash: {label}/{path}")
                if path in recorded_files:
                    raise ValueError(f"Publication proof repeats file: {label}/{path}")
                recorded_files[path] = digest
            if recorded_files != expected_files:
                raise ValueError(f"Publication proof file scope does not match: {label}")
            normalized_files = [dict(file) for file in files]
        normalized_repositories.append({**dict(item), "files": normalized_files})

    if seen_labels != set(expected_scope):
        missing = sorted(set(expected_scope) - seen_labels)
        raise ValueError(
            "Publication evidence does not cover every campaign repository: "
            + ", ".join(missing)
        )
    return {**dict(publication), "repositories": normalized_repositories}


def validate_decision_evidence(
    manifest: Mapping[str, Any],
    decision: Mapping[str, Any],
    publication: Mapping[str, Any] | None,
    *,
    campaign_manifest_sha256: str,
) -> dict[str, Any]:
    """Bind DataHub readback to the complete manifest and GitHub proof scope."""
    if decision.get("schemaVersion") != 1:
        raise ValueError("Decision evidence uses an unsupported schema version")
    if decision.get("campaignManifestSha256") != campaign_manifest_sha256:
        raise ValueError("Decision evidence does not match the campaign manifest")
    expected_assets = list(expected_decision_assets(manifest))
    assets = decision.get("assets")
    verified_assets = decision.get("verifiedAssets")
    if (
        decision.get("decision") != "approved-for-review"
        or decision.get("mergePolicy") != "manual-only"
        or decision.get("freshSessionReadback") is not True
        or assets != expected_assets
        or verified_assets != expected_assets
    ):
        raise ValueError("Decision evidence does not verify the complete covered asset set")
    if publication is None:
        raise ValueError("Decision evidence cannot be verified without publication proof")
    expected_pull_requests = sorted(
        item["pullRequest"]["url"] for item in publication["repositories"]
    )
    properties = decision.get("properties")
    if not isinstance(properties, Mapping):
        raise ValueError("Decision evidence does not contain structured properties")
    source = manifest.get("source")
    rename = source.get("rename") if isinstance(source, Mapping) else None
    if (
        not isinstance(source, Mapping)
        or not isinstance(rename, Mapping)
        or not isinstance(source.get("dataset"), str)
        or not isinstance(rename.get("oldColumn"), str)
        or not isinstance(rename.get("newColumn"), str)
    ):
        raise ValueError("Campaign manifest does not contain a complete source rename")
    approved_at = decision.get("approvedAt")
    if not isinstance(approved_at, str) or not approved_at:
        raise ValueError("Decision evidence does not contain an approval timestamp")
    expected_properties = {
        f"{PROPERTY_PREFIX}campaignManifestSha256": [campaign_manifest_sha256],
        f"{PROPERTY_PREFIX}reviewDecision": ["approved-for-review"],
        f"{PROPERTY_PREFIX}approvedAt": [approved_at],
        f"{PROPERTY_PREFIX}sourceChange": [
            f"{source['dataset']}.{rename['oldColumn']} -> {rename['newColumn']}"
        ],
        f"{PROPERTY_PREFIX}mergePolicy": ["manual-only"],
        f"{PROPERTY_PREFIX}pullRequests": expected_pull_requests,
    }
    for urn, expected in expected_properties.items():
        actual = properties.get(urn)
        if urn.endswith("pullRequests"):
            if not isinstance(actual, list) or sorted(actual) != expected:
                raise ValueError("Decision evidence pull requests do not match publication proof")
        elif actual != expected:
            raise ValueError(f"Decision evidence structured property does not match: {urn}")
    return dict(decision)


def _validate_verification_evidence(
    manifest: Mapping[str, Any],
    verification: Mapping[str, Any],
    *,
    campaign_manifest_sha256: str,
    publication: Mapping[str, Any] | None,
    decision: Mapping[str, Any] | None,
    postgres_validation: Mapping[str, Any] | None,
    context_verification: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if (
        verification.get("schemaVersion") != 1
        or verification.get("evidenceMode") != "captured-offline"
        or verification.get("campaignManifestSha256") != campaign_manifest_sha256
    ):
        raise ValueError("Verification evidence identity does not match this campaign")
    checks = verification.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("Verification evidence does not contain audit checks")
    check_map: dict[str, Mapping[str, Any]] = {}
    for check in checks:
        if (
            not isinstance(check, Mapping)
            or not isinstance(check.get("id"), str)
            or check.get("status") not in {"passed", "incomplete", "failed"}
            or not isinstance(check.get("label"), str)
            or not check["label"]
            or not isinstance(check.get("detail"), str)
            or not check["detail"]
        ):
            raise ValueError("Verification evidence contains an invalid audit check")
        check_id = check["id"]
        if check_id in check_map:
            raise ValueError(f"Verification evidence repeats audit check: {check_id}")
        check_map[check_id] = check
    if set(check_map) != AUDIT_CHECK_IDS:
        raise ValueError("Verification evidence does not cover the complete audit check set")

    statuses = [check["status"] for check in checks]
    expected_status = (
        "failed"
        if "failed" in statuses
        else "incomplete"
        if "incomplete" in statuses
        else "passed"
    )
    if verification.get("status") != expected_status:
        raise ValueError("Verification evidence status conflicts with its audit checks")
    evidence_requirements = {
        "github-delivery": publication,
        "datahub-decision": decision,
        "postgres-rollback": postgres_validation,
        "context-behavior": context_verification,
    }
    for check_id, evidence in evidence_requirements.items():
        if check_map[check_id]["status"] == "passed" and evidence is None:
            raise ValueError(
                f"Verification evidence marks {check_id} passed without its proof sidecar"
            )

    metrics = verification.get("metrics")
    expected_metrics = {
        "lineageAssets": len(manifest.get("lineageAssets") or ()),
        "targetRepositories": len(manifest.get("targetRepositories") or ()),
        "generatedFiles": len(manifest.get("downstreamPatches") or ())
        + len(manifest.get("contractPatches") or ()),
        "reviewRoutes": len(manifest.get("reviewAssignments") or ()),
        "publishedPullRequests": len(
            (publication or {}).get("repositories") or ()
        ),
        "decisionAssets": len((decision or {}).get("assets") or ()),
    }
    if metrics != expected_metrics:
        raise ValueError("Verification evidence metrics do not match the campaign proof scope")
    if not isinstance(verification.get("summary"), str) or not verification["summary"]:
        raise ValueError("Verification evidence does not contain an audit summary")
    return dict(verification)


def _read_relative(root: Path, relative: str) -> str:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"Bundle artifact escapes its directory: {relative}") from None
    if not candidate.is_file():
        raise ValueError(f"Bundle artifact does not exist: {relative}")
    return candidate.read_text(encoding="utf-8")


def load_campaign(bundle_dir: str | Path) -> dict[str, Any]:
    """Load one bundle and the text artifacts required by the review page."""
    root = Path(bundle_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Bundle does not contain manifest.json: {root}")
    manifest_bytes = manifest_path.read_bytes()
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bundle manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"Bundle manifest must contain an object: {manifest_path}")
    approved_hash = hashlib.sha256(manifest_bytes).hexdigest()

    context_verification = None
    context_path = root / "context-verification.json"
    if context_path.is_file():
        try:
            raw_context = json.loads(context_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Bundle context evidence is not valid JSON: {context_path}"
            ) from exc
        if not isinstance(raw_context, dict):
            raise ValueError("Bundle context evidence must contain an object")
        if raw_context.get("campaignManifestSha256") != approved_hash:
            raise ValueError("Bundle context evidence does not match the campaign manifest")
        try:
            context_verification = validate_context_verification_evidence(
                raw_context,
                campaign_manifest_sha256=approved_hash,
                captured=captured_context_from_manifest(manifest),
            )
        except ContextGuardError as exc:
            raise ValueError(f"Bundle context evidence is invalid: {exc}") from exc

    postgres_validation = None
    postgres_path = root / "postgres-validation.json"
    if postgres_path.is_file():
        try:
            raw_postgres = json.loads(postgres_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Bundle PostgreSQL evidence is not valid JSON: {postgres_path}"
            ) from exc
        if not isinstance(raw_postgres, dict):
            raise ValueError("Bundle PostgreSQL evidence must contain an object")
        source = manifest.get("source") or {}
        rename = source.get("rename") or {}
        migration_path = manifest.get("migration")
        if (
            not isinstance(source, dict)
            or not isinstance(rename, dict)
            or not isinstance(migration_path, str)
        ):
            raise ValueError("Bundle manifest does not contain a complete source migration")
        try:
            postgres_validation = validate_postgres_evidence(
                raw_postgres,
                campaign_manifest_sha256=approved_hash,
                migration_sql=_read_relative(root, migration_path),
                source_dataset=str(source.get("dataset") or ""),
                old_column=str(rename.get("oldColumn") or ""),
                new_column=str(rename.get("newColumn") or ""),
            )
        except ValueError as exc:
            raise ValueError(f"Bundle PostgreSQL evidence is invalid: {exc}") from exc

    publication = None
    publication_path = root / "publication.json"
    if publication_path.is_file():
        try:
            raw_publication = json.loads(publication_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Bundle publication evidence is not valid JSON: {publication_path}"
            ) from exc
        if not isinstance(raw_publication, dict):
            raise ValueError("Bundle publication evidence must contain an object")
        try:
            publication = validate_publication_evidence(
                manifest,
                raw_publication,
                campaign_manifest_sha256=approved_hash,
            )
        except ValueError as exc:
            raise ValueError(f"Bundle publication evidence is invalid: {exc}") from exc

    decision = None
    decision_path = root / "decision.json"
    if decision_path.is_file():
        try:
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Bundle decision evidence is not valid JSON: {decision_path}"
            ) from exc
        if not isinstance(decision, dict):
            raise ValueError("Bundle decision evidence must contain an object")
        try:
            decision = validate_decision_evidence(
                manifest,
                decision,
                publication,
                campaign_manifest_sha256=approved_hash,
            )
        except ValueError as exc:
            raise ValueError(f"Bundle decision evidence is invalid: {exc}") from exc

    verification = None
    verification_path = root / "verification.json"
    if verification_path.is_file():
        try:
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Bundle verification evidence is not valid JSON: {verification_path}"
            ) from exc
        if not isinstance(verification, dict):
            raise ValueError("Bundle verification evidence must contain an object")
        try:
            verification = _validate_verification_evidence(
                manifest,
                verification,
                campaign_manifest_sha256=approved_hash,
                publication=publication,
                decision=decision,
                postgres_validation=postgres_validation,
                context_verification=context_verification,
            )
        except ValueError as exc:
            raise ValueError(f"Bundle verification evidence is invalid: {exc}") from exc

    artifacts = []
    for kind, key in (("SQL model", "downstreamPatches"), ("dbt contract", "contractPatches")):
        entries = manifest.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"Bundle manifest field {key!r} must contain a list")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("diffPath"), str):
                raise ValueError(f"Bundle manifest contains an invalid {key} entry")
            artifacts.append(
                {
                    "kind": kind,
                    **entry,
                    "assetUrns": list(patch_asset_urns(entry)),
                    "assetNames": list(patch_asset_names(entry)),
                    "diff": _read_relative(root, entry["diffPath"]),
                }
            )

    migration_path = manifest.get("migration")
    report_path = manifest.get("impactReport")
    if not isinstance(migration_path, str) or not isinstance(report_path, str):
        raise ValueError("Bundle manifest must identify migration and impact report files")
    return {
        "manifest": manifest,
        "migrationSql": _read_relative(root, migration_path),
        "impactReport": _read_relative(root, report_path),
        "artifacts": artifacts,
        "contextVerification": context_verification,
        "postgresValidationEvidence": postgres_validation,
        "publication": publication,
        "decision": decision,
        "verification": verification,
    }


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def render_replay_html(campaigns: Sequence[dict[str, Any]]) -> str:
    """Return a dependency-free campaign review page with embedded evidence."""
    if not campaigns:
        raise ValueError("At least one campaign is required")
    payload = _json_for_script(list(campaigns))
    SITE_CSS = design.SITE_CSS
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2308090a'/%3E%3Cpath d='M17 46V18h16c9 0 14 5 14 12 0 5-3 9-8 11l9 5H37l-8-5h-2v5H17zm10-14h6c3 0 5-1 5-3s-2-3-5-3h-6v6z' fill='%234cc38a'/%3E%3C/svg%3E">
  <title>RippleProof campaign review</title>
  <style>
{SITE_CSS}

    /* This page keeps its own semantic names, bound to the shared palette, so a
       generated campaign reads identically to the rest of the site without
       rewriting every rule below. The CSS above is inlined rather than linked
       because a campaign is written into whatever output directory the operator
       chose and must survive being copied elsewhere. */
    :root {{
      --ink-soft: var(--ink-2);
      --paper: var(--ground);
      --surface-strong: var(--raised);
      --line-strong: var(--line-2);
      --primary: var(--mint);
      --primary-soft: var(--mint-wash);
      --success: var(--mint);
      --success-soft: rgba(76, 195, 138, 0.13);
      --warning: var(--amber);
      --warning-soft: var(--amber-wash);
      --danger: var(--red);
      --danger-soft: rgba(229, 83, 75, 0.13);
      --manual: var(--indigo);
      --manual-soft: rgba(139, 149, 232, 0.14);
      --shadow: 0 18px 45px rgba(0, 0, 0, 0.45);
      --radius: 12px;
    }}

    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font: 16px/1.55 var(--sans);
    }}
    button, a {{ touch-action: manipulation; }}
    button {{ font: inherit; }}
    button:focus-visible, a:focus-visible, summary:focus-visible {{
      outline: 3px solid #3a8fc2;
      outline-offset: 3px;
    }}
    .skip-link {{
      position: fixed;
      left: 16px;
      top: 12px;
      z-index: 10;
      transform: translateY(-160%);
      background: var(--ink);
      color: var(--surface);
      padding: 10px 14px;
      border-radius: 8px;
    }}
    .skip-link:focus {{ transform: translateY(0); }}
    main {{ width: min(100% - 40px, var(--w)); margin: 0 auto; padding: 28px 0 64px; }}
    .campaign-header {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 28px;
      align-items: end;
      padding: 30px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
      box-shadow: var(--shadow);
    }}
    .status-line {{ display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 14px; }}
    .status {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 4px 10px;
      border-radius: 6px;
      background: var(--success-soft);
      color: var(--success);
      font: 700 12px/1.2 var(--sans);
      letter-spacing: 0.04em;
      text-transform: uppercase;
    }}
    .context-source {{ color: var(--ink-soft); font: 600 13px/1.2 var(--mono); }}
    h1, h2, h3, p {{ margin-top: 0; }}
    h1 {{ margin-bottom: 8px; font-size: clamp(30px, 4vw, 50px); line-height: 1.04; letter-spacing: -0.035em; }}
    h2 {{ margin-bottom: 4px; font-size: 21px; line-height: 1.25; letter-spacing: -0.015em; }}
    h3 {{ margin-bottom: 5px; font-size: 16px; line-height: 1.35; }}
    .lede {{ max-width: 68ch; margin-bottom: 20px; color: var(--ink-soft); font-size: 17px; }}
    .rename {{
      width: fit-content;
      max-width: 100%;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-strong);
      font: 650 14px/1.4 var(--mono);
    }}
    .rename .old {{ color: var(--danger); }}
    .rename .arrow {{ color: var(--ink-soft); }}
    .rename .new {{ color: var(--success); }}
    .mode-picker {{ min-width: 260px; }}
    .mode-label {{ margin-bottom: 8px; color: var(--ink-soft); font-size: 13px; font-weight: 650; }}
    .tabs {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    .tab {{
      min-height: 44px;
      padding: 9px 14px;
      border: 1px solid var(--line-strong);
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
      cursor: pointer;
      font-weight: 650;
      transition: background 160ms ease, border-color 160ms ease, color 160ms ease;
    }}
    .tab:hover {{ border-color: var(--primary); }}
    .tab[aria-selected="true"] {{ border-color: var(--primary); background: var(--primary); color: var(--surface); }}
    .metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin: 14px 0; }}
    .metric {{ padding: 16px 18px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); }}
    .metric-label {{ color: var(--ink-soft); font-size: 13px; font-weight: 650; }}
    .metric-value {{ display: block; margin: 3px 0 1px; font: 750 28px/1.1 var(--mono); }}
    .metric-detail {{ color: var(--ink-soft); font-size: 12px; }}
    .audit-panel {{
      margin-bottom: 14px;
      border: 1px solid var(--line);
      border-left: 5px solid var(--line-strong);
      border-radius: var(--radius);
      background: var(--surface);
      overflow: hidden;
    }}
    .audit-panel[data-status="passed"] {{ border-left-color: var(--success); }}
    .audit-panel[data-status="incomplete"] {{ border-left-color: var(--warning); }}
    .audit-panel[data-status="failed"] {{ border-left-color: var(--danger); }}
    .audit-overview {{
      min-height: 92px;
      padding: 18px 20px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: center;
    }}
    .audit-kicker {{
      display: block;
      margin-bottom: 5px;
      color: var(--ink-soft);
      font: 700 11px/1.2 var(--mono);
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .audit-summary {{ margin: 5px 0 0; color: var(--ink-soft); }}
    .audit-score {{ min-width: 142px; text-align: right; }}
    .audit-score strong {{ display: block; font: 750 28px/1.1 var(--mono); }}
    .audit-score span {{ color: var(--ink-soft); font-size: 12px; }}
    .audit-details {{ border: 0; border-top: 1px solid var(--line); border-radius: 0; }}
    .audit-details summary {{
      min-height: 48px;
      padding: 12px 20px;
      display: flex;
      align-items: center;
      color: var(--primary);
    }}
    .audit-checks {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      padding: 0 20px 20px;
    }}
    .audit-check {{ padding: 11px 12px; border: 1px solid var(--line); border-left: 4px solid var(--line-strong); border-radius: 8px; }}
    .audit-check[data-status="passed"] {{ border-left-color: var(--success); }}
    .audit-check[data-status="incomplete"] {{ border-left-color: var(--warning); }}
    .audit-check[data-status="failed"] {{ border-left-color: var(--danger); }}
    .audit-check-title {{ display: block; font-weight: 750; }}
    .audit-check-detail {{ display: block; margin-top: 3px; color: var(--ink-soft); font-size: 13px; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1.55fr) minmax(320px, 0.85fr); gap: 14px; align-items: start; }}
    .stack {{ display: grid; gap: 14px; }}
    .panel {{ border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); overflow: hidden; }}
    .panel-head {{
      min-height: 68px;
      padding: 15px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      border-bottom: 1px solid var(--line);
    }}
    .panel-head p {{ margin: 0; color: var(--ink-soft); font-size: 13px; }}
    .panel-body {{ padding: 18px; }}
    .mode-summary {{
      margin-bottom: 14px;
      padding: 14px 16px;
      border-left: 4px solid var(--primary);
      background: var(--primary-soft);
      color: var(--ink);
      font-weight: 600;
    }}
    .lineage-levels {{ display: grid; grid-template-columns: minmax(170px, 0.8fr) repeat(var(--hop-count, 1), minmax(190px, 1fr)); gap: 28px; align-items: stretch; }}
    .lineage-level {{ position: relative; display: grid; align-content: center; gap: 10px; }}
    .lineage-level + .lineage-level::before {{
      content: "";
      position: absolute;
      width: 20px;
      height: 1px;
      left: -24px;
      top: 50%;
      background: var(--line-strong);
    }}
    .lineage-level + .lineage-level::after {{
      content: ">";
      position: absolute;
      left: -10px;
      top: calc(50% - 10px);
      color: var(--line-strong);
      font: 700 14px/20px var(--mono);
    }}
    .level-label {{ color: var(--ink-soft); font: 650 11px/1.2 var(--mono); letter-spacing: 0.05em; text-transform: uppercase; }}
    .asset {{
      width: 100%;
      min-height: 76px;
      padding: 12px 13px;
      text-align: left;
      border: 1px solid var(--line-strong);
      border-left-width: 4px;
      border-radius: 8px;
      background: var(--surface);
      color: var(--ink);
    }}
    button.asset {{ cursor: pointer; transition: background 160ms ease, border-color 160ms ease; }}
    button.asset:hover {{ background: var(--surface-strong); border-color: var(--primary); }}
    .asset[data-state="patched"] {{ border-left-color: var(--success); }}
    .asset[data-state="protected"] {{ border-left-color: var(--primary); }}
    .asset[data-state="manual"] {{ border-left-color: var(--manual); }}
    .asset[data-state="source"] {{ border-left-color: var(--danger); background: var(--danger-soft); }}
    .asset-name {{ display: block; overflow-wrap: anywhere; font-weight: 750; }}
    .asset-meta {{ display: block; margin-top: 3px; color: var(--ink-soft); font-size: 12px; }}
    .asset-state {{ display: block; margin-top: 7px; font: 700 11px/1.2 var(--mono); text-transform: uppercase; }}
    .asset-detail {{ margin-top: 14px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface-strong); }}
    .asset-detail p {{ margin-bottom: 0; color: var(--ink-soft); }}
    .legend {{ display: flex; flex-wrap: wrap; gap: 14px; margin-top: 14px; color: var(--ink-soft); font-size: 12px; }}
    .legend-item {{ display: inline-flex; align-items: center; gap: 6px; }}
    .swatch {{ width: 10px; height: 10px; border-radius: 2px; background: var(--line-strong); }}
    .swatch.patched {{ background: var(--success); }}
    .swatch.protected {{ background: var(--primary); }}
    .swatch.manual {{ background: var(--manual); }}
    .evidence-list, .validation-list, .safety-list {{ display: grid; gap: 10px; margin: 0; padding: 0; list-style: none; }}
    .evidence-item, .validation-item, .safety-item {{ min-width: 0; padding: 11px 12px; border: 1px solid var(--line); border-radius: 8px; }}
    .routing-item {{ padding: 11px 12px; border: 1px solid var(--line); border-left: 4px solid var(--line-strong); border-radius: 8px; }}
    .routing-item[data-status="assigned"] {{ border-left-color: var(--success); }}
    .routing-item[data-status="unassigned"] {{ border-left-color: var(--warning); }}
    .evidence-tool {{ display: block; color: var(--primary); font: 700 13px/1.3 var(--mono); }}
    .evidence-detail, .validation-detail, .safety-detail {{ display: block; margin-top: 3px; overflow-wrap: anywhere; color: var(--ink-soft); font-size: 13px; }}
    .execution-summary {{ margin-bottom: 10px; font-weight: 750; }}
    .execution-project {{ padding: 12px; border: 1px solid var(--line); border-left: 4px solid var(--line-strong); border-radius: 8px; }}
    .execution-project[data-status="passed"] {{ border-left-color: var(--success); }}
    .execution-project[data-status="failed"] {{ border-left-color: var(--danger); }}
    .execution-step {{ margin-top: 9px; padding-top: 9px; border-top: 1px solid var(--line); }}
    .execution-command {{ display: block; margin-top: 4px; overflow-wrap: anywhere; color: var(--ink-soft); font: 11px/1.45 var(--mono); }}
    .delivery-repo {{ padding: 12px; border: 1px solid var(--line); border-left: 4px solid var(--line-strong); border-radius: 8px; }}
    .delivery-repo[data-status="passed"] {{ border-left-color: var(--success); }}
    .delivery-repo[data-status="failed"] {{ border-left-color: var(--danger); }}
    .delivery-link {{ display: inline-flex; min-height: 44px; margin-top: 3px; align-items: center; color: var(--primary); font-weight: 750; text-decoration-thickness: 1px; text-underline-offset: 3px; overflow-wrap: anywhere; }}
    .delivery-link:focus-visible {{ border-radius: 2px; }}
    .decision-card {{ padding: 14px; border: 1px solid var(--line); border-left: 4px solid var(--success); border-radius: 8px; background: var(--success-soft); }}
    .decision-status {{ display: block; color: var(--success); font: 750 13px/1.3 var(--mono); text-transform: uppercase; }}
    .decision-grid {{ display: grid; gap: 8px; margin-top: 12px; }}
    .decision-row {{ display: grid; gap: 2px; padding-top: 8px; border-top: 1px solid rgba(23, 107, 77, 0.24); }}
    .decision-label {{ color: var(--ink-soft); font-size: 12px; font-weight: 700; }}
    .decision-value {{ overflow-wrap: anywhere; color: var(--ink); font: 12px/1.45 var(--mono); }}
    .validation-item {{ border-left-width: 4px; }}
    .validation-item[data-status="passed"] {{ border-left-color: var(--success); }}
    .validation-item[data-status="warning"] {{ border-left-color: var(--warning); }}
    .validation-item[data-status="failed"] {{ border-left-color: var(--danger); }}
    .validation-name {{ font-weight: 750; }}
    .validation-status {{ margin-left: 7px; font: 700 11px/1 var(--mono); text-transform: uppercase; }}
    .artifacts-layout {{ display: grid; grid-template-columns: minmax(230px, 0.6fr) minmax(0, 1.4fr); min-height: 420px; }}
    .artifact-list {{ padding: 12px; border-right: 1px solid var(--line); background: var(--surface-strong); }}
    .artifact-button {{
      width: 100%;
      min-height: 58px;
      margin-bottom: 8px;
      padding: 10px 12px;
      text-align: left;
      border: 1px solid transparent;
      border-radius: 8px;
      background: transparent;
      color: var(--ink);
      cursor: pointer;
    }}
    .artifact-button:hover {{ border-color: var(--line-strong); background: var(--surface); }}
    .artifact-button[aria-selected="true"] {{ border-color: var(--primary); background: var(--primary-soft); }}
    .artifact-title {{ display: block; overflow-wrap: anywhere; font-weight: 700; }}
    .artifact-meta {{ display: block; margin-top: 3px; color: var(--ink-soft); font-size: 12px; }}
    .diff-wrap {{ min-width: 0; background: #0a0b0c; color: var(--ink); }}
    .diff-toolbar {{ min-height: 52px; padding: 11px 16px; display: flex; align-items: center; justify-content: space-between; gap: 14px; border-bottom: 1px solid #344656; }}
    .diff-path {{ overflow-wrap: anywhere; font: 650 12px/1.4 var(--mono); }}
    .diff-count {{ color: var(--ink-soft); font: 600 12px/1.2 var(--mono); white-space: nowrap; }}
    pre {{ margin: 0; }}
    .diff {{ max-height: 520px; overflow: auto; padding: 14px 0 20px; font: 12px/1.55 var(--mono); tab-size: 4; }}
    .diff-line {{ display: block; min-width: max-content; padding: 0 16px; white-space: pre; }}
    .diff-line.add {{ background: rgba(76, 195, 138, 0.13); color: #9be3c0; }}
    .diff-line.remove {{ background: rgba(235, 87, 87, 0.13); color: #f0a1a1; }}
    .diff-line.header {{ color: var(--ink-soft); }}
    .migration {{ padding: 14px 16px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: #0a0b0c; color: var(--ink); font: 13px/1.55 var(--mono); }}
    details {{ border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }}
    summary {{ min-height: 44px; padding: 10px 13px; cursor: pointer; font-weight: 700; }}
    .report {{ max-height: 360px; overflow: auto; padding: 0 14px 14px; color: var(--ink-soft); font: 13px/1.55 var(--mono); white-space: pre-wrap; }}
    .empty {{ min-height: 220px; display: grid; place-items: center; padding: 28px; text-align: center; color: var(--ink-soft); }}
    .footer {{ margin-top: 16px; color: var(--ink-soft); font-size: 13px; text-align: center; }}

    @media (max-width: 1140px) {{
      .campaign-header {{ grid-template-columns: 1fr; align-items: start; }}
      .mode-picker {{ min-width: 0; }}
      .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .audit-checks {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: 1fr; }}
      .lineage-levels {{ grid-template-columns: 1fr; gap: 22px; }}
      .lineage-level + .lineage-level::before {{ width: 1px; height: 16px; left: 22px; top: -19px; }}
      .lineage-level + .lineage-level::after {{ content: "v"; left: 17px; top: -12px; }}
    }}
    @media (max-width: 700px) {{
      main {{ width: min(100% - 24px, var(--w)); }}
      .top-note {{ display: none; }}
      .top-links a {{ padding: 5px 8px; font-size: 12.5px; }}
      main {{ padding-top: 14px; }}
      .campaign-header {{ padding: 20px; }}
      .metrics {{ grid-template-columns: 1fr 1fr; gap: 8px; }}
      .audit-overview {{ grid-template-columns: 1fr; gap: 12px; }}
      .audit-score {{ text-align: left; }}
      .metric {{ padding: 13px; }}
      .metric-value {{ font-size: 23px; }}
      .panel-head {{ align-items: flex-start; flex-direction: column; gap: 4px; }}
      .panel-body {{ padding: 14px; }}
      .artifacts-layout {{ grid-template-columns: 1fr; }}
      .artifact-list {{ border-right: 0; border-bottom: 1px solid var(--line); }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      html {{ scroll-behavior: auto; }}
      *, *::before, *::after {{ transition-duration: 0.01ms !important; animation-duration: 0.01ms !important; }}
    }}
    @media print {{
      .nav, .mode-picker, .artifact-list {{ display: none; }}
      body {{ background: var(--surface); }}
      main {{ width: 100%; padding: 0; }}
      .campaign-header, .panel, .metric {{ box-shadow: none; break-inside: avoid; }}
      .grid {{ grid-template-columns: 1fr; }}
      .artifacts-layout {{ grid-template-columns: 1fr; }}
      .diff {{ max-height: none; }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#content">Skip to campaign</a>
  <header class="nav">
    <div class="wrap">
      <a class="brand" href="../../index.html"><svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#4cc38a"/><circle cx="16" cy="16" r="3.6" fill="#08090a"/><circle cx="16" cy="16" r="8.4" fill="none" stroke="#08090a" stroke-width="1.9"/></svg> RippleProof</a>
      <nav class="steps" aria-label="Primary">
        <a href="../../index.html"><b>&larr;</b> Home</a>
        <a href="../index.html"><b>&#9632;</b> All examples</a>
        <a href="../run.html"><b>01</b> The run</a>
        <a href="../workspace.html"><b>02</b> Review</a>
        <a href="campaign.html" aria-current="page"><b>03</b> Evidence</a>
      </nav>
    </div>
  </header>
  <main id="content">
    <section class="campaign-header" aria-labelledby="campaign-title">
      <div>
        <div class="status-line"><span class="status" id="status">Ready for review</span><span class="context-source" id="context-source"></span></div>
        <h1 id="campaign-title">Change campaign</h1>
        <p class="lede" id="lede"></p>
        <div class="rename" aria-label="Proposed column rename"><span class="old" id="old-column"></span><span class="arrow" aria-hidden="true">-&gt;</span><span class="new" id="new-column"></span></div>
      </div>
      <div class="mode-picker" id="mode-picker">
        <div class="mode-label">Migration policy</div>
        <div class="tabs" id="tabs" role="tablist" aria-label="Campaign mode"></div>
      </div>
    </section>

    <section class="metrics" aria-label="Campaign summary" id="metrics"></section>

    <section class="audit-panel" id="audit-panel" data-status="incomplete" aria-labelledby="audit-title">
      <div class="audit-overview">
        <div>
          <span class="audit-kicker">Captured-evidence consistency check</span>
          <h2 id="audit-title">Campaign audit</h2>
          <p class="audit-summary" id="audit-summary"></p>
        </div>
        <div class="audit-score"><strong id="audit-score">Not recorded</strong><span id="audit-scope">Internal consistency only</span></div>
      </div>
      <details class="audit-details" id="audit-details">
        <summary id="audit-toggle">Show audit checks</summary>
        <div class="audit-checks" id="audit-checks"></div>
      </details>
    </section>

    <div class="grid">
      <div class="stack">
        <section class="panel" aria-labelledby="lineage-title">
          <div class="panel-head"><div><h2 id="lineage-title">Lineage and dispositions</h2><p>Every reachable asset receives an explicit outcome.</p></div><span class="context-source" id="hop-depth"></span></div>
          <div class="panel-body">
            <div class="mode-summary" id="mode-summary"></div>
            <div class="lineage-levels" id="lineage-levels"></div>
            <div class="asset-detail" id="asset-detail" aria-live="polite"></div>
            <div class="legend" aria-label="Asset disposition legend"><span class="legend-item"><span class="swatch patched"></span>Patched</span><span class="legend-item"><span class="swatch protected"></span>Protected by contract</span><span class="legend-item"><span class="swatch manual"></span>Manual review</span></div>
          </div>
        </section>

        <section class="panel" aria-labelledby="artifacts-title">
          <div class="panel-head"><div><h2 id="artifacts-title">Generated changes</h2><p>Complete patches, not suggested snippets.</p></div><span class="context-source" id="artifact-total"></span></div>
          <div class="artifacts-layout" id="artifacts-layout">
            <div class="artifact-list" id="artifact-list" role="tablist" aria-label="Generated artifacts"></div>
            <div class="diff-wrap" id="diff-wrap" role="tabpanel"></div>
          </div>
        </section>
      </div>

      <aside class="stack" aria-label="Campaign evidence">
        <section class="panel" aria-labelledby="evidence-title">
          <div class="panel-head"><div><h2 id="evidence-title">DataHub evidence</h2><p>The context reads behind this campaign.</p></div></div>
          <div class="panel-body"><ol class="evidence-list" id="evidence-list"></ol></div>
        </section>
        <section class="panel" aria-labelledby="routing-title">
          <div class="panel-head"><div><h2 id="routing-title">Review routing</h2><p>DataHub owners matched to every campaign disposition.</p></div></div>
          <div class="panel-body"><div class="execution-summary" id="routing-summary"></div><div class="evidence-list" id="routing-list"></div></div>
        </section>
        <section class="panel" aria-labelledby="mapping-title">
          <div class="panel-head"><div><h2 id="mapping-title">Model mapping decisions</h2><p>Explicit reviewer choices for ambiguous DataHub assets.</p></div></div>
          <div class="panel-body"><div class="evidence-list" id="mapping-list"></div></div>
        </section>
        <section class="panel" aria-labelledby="decision-title">
          <div class="panel-head"><div><h2 id="decision-title">DataHub review decision</h2><p>Structured approval state read back through a fresh client.</p></div></div>
          <div class="panel-body" id="decision-body"></div>
        </section>
        <section class="panel" aria-labelledby="validation-title">
          <div class="panel-head"><div><h2 id="validation-title">Validation gates</h2><p>Review readiness is deterministic.</p></div></div>
          <div class="panel-body"><ul class="validation-list" id="validation-list"></ul></div>
        </section>
        <section class="panel" aria-labelledby="execution-title">
          <div class="panel-head"><div><h2 id="execution-title">Executable dbt validation</h2><p>Proposed repositories built against disposable DuckDB data.</p></div></div>
          <div class="panel-body"><div class="execution-summary" id="execution-summary"></div><div class="evidence-list" id="execution-list"></div></div>
        </section>
        <section class="panel" aria-labelledby="postgres-title">
          <div class="panel-head"><div><h2 id="postgres-title">Disposable PostgreSQL validation</h2><p>The constrained source rename applied and rolled back in an isolated database.</p></div></div>
          <div class="panel-body"><div class="execution-summary" id="postgres-summary"></div><div class="evidence-list" id="postgres-list"></div></div>
        </section>
        <section class="panel" aria-labelledby="delivery-title">
          <div class="panel-head"><div><h2 id="delivery-title">GitHub delivery</h2><p>Review-only pull requests linked to the approved manifest.</p></div></div>
          <div class="panel-body"><div class="execution-summary" id="delivery-summary"></div><div class="evidence-list" id="delivery-list"></div></div>
        </section>
        <section class="panel" aria-labelledby="migration-title">
          <div class="panel-head"><div><h2 id="migration-title">Proposed migration</h2><p>Never executed against the source; optional validation is disposable and rolled back.</p></div></div>
          <div class="panel-body"><pre class="migration" id="migration"></pre></div>
        </section>
        <section class="panel" aria-labelledby="safety-title">
          <div class="panel-head"><div><h2 id="safety-title">Safety boundary</h2><p>What RippleProof did and did not change.</p></div></div>
          <div class="panel-body"><ul class="safety-list" id="safety-list"></ul></div>
        </section>
        <details><summary>Read the full impact report</summary><pre class="report" id="impact-report"></pre></details>
      </aside>
    </div>
    <p class="footer">Self-contained campaign review generated by RippleProof.</p>
  </main>
  <script id="campaign-data" type="application/json">{payload}</script>
  <script>
    (() => {{
      const campaigns = JSON.parse(document.getElementById('campaign-data').textContent);
      let activeCampaign = 0;
      let activeArtifact = 0;
      let activeAsset = null;

      const byId = (id) => document.getElementById(id);
      const create = (tag, className, text) => {{
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined) node.textContent = text;
        return node;
      }};
      const modeLabel = (mode) => mode === 'propagate' ? 'Propagate rename' : 'Preserve contract';
      const campaignLabel = (campaign) => {{
        const manifest = campaign.manifest;
        const base = modeLabel(manifest.downstreamColumnMode);
        const sameModeCount = campaigns.filter(
          (item) => item.manifest.downstreamColumnMode === manifest.downstreamColumnMode
        ).length;
        if (sameModeCount < 2) return base;
        if ((manifest.modelMappingResolutions || []).length) return `${{base}} · resolved`;
        return `${{base}} · ${{manifest.readyForReview ? 'ready' : 'blocked'}}`;
      }};
      const sourceName = (manifest) => manifest.source?.dataset || manifest.source?.urn || 'Source dataset';
      const rename = (manifest) => manifest.source?.rename || {{ oldColumn: 'old column', newColumn: 'new column' }};
      // Compatibility with bundles created before coalesced patch asset arrays.
      const patchUrns = (item) => item.assetUrns || (item.assetUrn ? [item.assetUrn] : []);
      const patchNames = (item) => item.assetNames || (item.assetName ? [item.assetName] : []);

      function dispositionMap(manifest) {{
        const result = new Map();
        for (const item of manifest.downstreamPatches || []) {{
          for (const urn of patchUrns(item)) result.set(urn, {{ state: 'patched', label: 'Patched', reason: item.repository + '/' + item.sourcePath }});
        }}
        for (const item of manifest.protectedByContract || []) result.set(item.assetUrn, {{ state: 'protected', label: 'Protected', reason: item.reason }});
        for (const item of manifest.manualReview || []) result.set(item.assetUrn, {{ state: 'manual', label: 'Manual review', reason: item.reason }});
        return result;
      }}

      function renderTabs() {{
        const tabs = byId('tabs');
        tabs.replaceChildren();
        campaigns.forEach((campaign, index) => {{
          const button = create('button', 'tab', campaignLabel(campaign));
          button.type = 'button';
          button.role = 'tab';
          button.id = `campaign-tab-${{index}}`;
          button.setAttribute('aria-selected', String(index === activeCampaign));
          button.tabIndex = index === activeCampaign ? 0 : -1;
          button.addEventListener('click', () => {{ activeCampaign = index; activeArtifact = 0; activeAsset = null; render(); }});
          button.addEventListener('keydown', (event) => {{
            if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
            event.preventDefault();
            const direction = event.key === 'ArrowRight' ? 1 : -1;
            activeCampaign = (activeCampaign + direction + campaigns.length) % campaigns.length;
            activeArtifact = 0;
            activeAsset = null;
            render();
            byId(`campaign-tab-${{activeCampaign}}`).focus();
          }});
          tabs.append(button);
        }});
        byId('mode-picker').hidden = campaigns.length < 2;
      }}

      function renderMetrics(campaign) {{
        const manifest = campaign.manifest;
        const assets = manifest.lineageAssets || [];
        const codePatches = (manifest.downstreamPatches || []).length;
        const contractPatches = (manifest.contractPatches || []).length;
        const routes = manifest.reviewAssignments || [];
        const repositoryCandidates = manifest.targetRepositories || [];
        const selectedRepositoryNames = new Set([
          ...(manifest.downstreamPatches || []),
          ...(manifest.contractPatches || []),
        ].map((patch) => patch.repository).filter(Boolean));
        const selectedRepositories = repositoryCandidates.filter((name) => selectedRepositoryNames.has(name));
        const unselectedRepositories = repositoryCandidates.filter((name) => !selectedRepositoryNames.has(name));
        const values = [
          ['Reachable assets', assets.length, `${{assets.filter((asset) => asset.evidence === 'column').length}} column-confirmed`],
          ['Repository candidates', repositoryCandidates.length, `${{selectedRepositories.length}} selected · ${{unselectedRepositories.length}} unselected`],
          ['Generated patches', codePatches + contractPatches, `${{codePatches}} SQL, ${{contractPatches}} contract`],
          ['Manual review', (manifest.manualReview || []).length, 'uncertain assets stay visible'],
          ['Assigned routes', routes.filter((route) => route.routingStatus === 'assigned').length, `${{routes.length}} source + downstream`],
        ];
        const root = byId('metrics');
        root.replaceChildren();
        for (const [label, value, detail] of values) {{
          const card = create('article', 'metric');
          card.append(create('span', 'metric-label', label), create('strong', 'metric-value', String(value)), create('span', 'metric-detail', detail));
          root.append(card);
        }}
      }}

      function renderAudit(campaign) {{
        const audit = campaign.verification;
        const panel = byId('audit-panel');
        const summary = byId('audit-summary');
        const score = byId('audit-score');
        const scope = byId('audit-scope');
        const details = byId('audit-details');
        const toggle = byId('audit-toggle');
        const checksRoot = byId('audit-checks');
        details.open = false;
        checksRoot.replaceChildren();
        if (!audit) {{
          panel.dataset.status = 'incomplete';
          score.textContent = 'Not recorded';
          scope.textContent = 'Internal consistency only';
          summary.textContent = 'This view contains captured campaign evidence, but no offline consistency sidecar was recorded. External authenticity and freshness are not established.';
          toggle.textContent = 'No audit checks available';
          return;
        }}
        const checks = audit.checks || [];
        const passed = checks.filter((check) => check.status === 'passed').length;
        panel.dataset.status = audit.status;
        score.textContent = `${{passed}} / ${{checks.length}}`;
        scope.textContent = audit.status === 'passed' ? 'Captured checks internally consistent' : `Audit ${{audit.status}}`;
        summary.textContent = `${{audit.summary}} This offline check does not establish external authenticity or freshness.`;
        toggle.textContent = `Show ${{checks.length}} audit checks`;
        for (const check of checks) {{
          const item = create('article', 'audit-check');
          item.dataset.status = check.status;
          item.append(
            create('span', 'audit-check-title', `${{check.status.toUpperCase()}} · ${{check.label}}`),
            create('span', 'audit-check-detail', check.detail),
          );
          checksRoot.append(item);
        }}
      }}

      function renderAssetDetail(asset, disposition) {{
        const detail = byId('asset-detail');
        detail.replaceChildren();
        detail.append(create('h3', '', `${{asset.name}}: ${{disposition.label}}`));
        const evidence = asset.evidence === 'column'
          ? `DataHub confirmed column lineage for ${{(asset.columns || []).join(', ') || 'the changed field'}} at hop ${{asset.degree}}.`
          : `DataHub confirmed asset reachability at hop ${{asset.degree}}, but not use of the changed column.`;
        detail.append(create('p', '', `${{evidence}} ${{disposition.reason}}`));
        const reviewers = (asset.owners || []).map((owner) => owner.name || owner.urn);
        detail.append(create('p', '', reviewers.length ? `Reviewers: ${{reviewers.join(', ')}}.` : 'Review owner: unassigned in DataHub.'));
      }}

      function renderLineage(campaign) {{
        const manifest = campaign.manifest;
        const assets = manifest.lineageAssets || [];
        const dispositions = dispositionMap(manifest);
        const maxHop = Math.max(1, ...assets.map((asset) => Number(asset.degree) || 1));
        byId('hop-depth').textContent = `${{maxHop}} hop${{maxHop === 1 ? '' : 's'}} checked`;
        const levels = byId('lineage-levels');
        levels.replaceChildren();
        levels.style.setProperty('--hop-count', maxHop);

        const sourceLevel = create('div', 'lineage-level');
        sourceLevel.append(create('span', 'level-label', 'Source'));
        const source = create('div', 'asset');
        source.dataset.state = 'source';
        source.append(create('span', 'asset-name', sourceName(manifest)), create('span', 'asset-meta', `${{manifest.source?.platform || 'dataset'}} source`), create('span', 'asset-state', 'Proposed rename'));
        sourceLevel.append(source);
        levels.append(sourceLevel);

        for (let hop = 1; hop <= maxHop; hop += 1) {{
          const level = create('div', 'lineage-level');
          level.append(create('span', 'level-label', `Hop ${{hop}}`));
          const atHop = assets.filter((asset) => Number(asset.degree) === hop);
          for (const asset of atHop) {{
            const disposition = dispositions.get(asset.urn) || {{ state: 'manual', label: 'Manual review', reason: 'No disposition was recorded.' }};
            const button = create('button', 'asset');
            button.type = 'button';
            button.dataset.state = disposition.state;
            button.setAttribute('aria-label', `${{asset.name}}, ${{disposition.label}}`);
            button.append(create('span', 'asset-name', asset.name), create('span', 'asset-meta', `${{asset.platform}} · ${{asset.type.toLowerCase()}} · ${{asset.evidence}} evidence`), create('span', 'asset-state', disposition.label));
            button.addEventListener('click', () => {{ activeAsset = asset.urn; renderAssetDetail(asset, disposition); }});
            level.append(button);
          }}
          levels.append(level);
        }}

        const selected = assets.find((asset) => asset.urn === activeAsset) || assets[0];
        if (selected) renderAssetDetail(selected, dispositions.get(selected.urn) || {{ label: 'Manual review', reason: 'No disposition was recorded.' }});
        else {{
          byId('asset-detail').replaceChildren(create('h3', '', 'No downstream lineage returned'), create('p', '', 'An empty graph is not proof that the change is safe. Dependencies may be missing from DataHub.'));
        }}

        const mode = manifest.downstreamColumnMode;
        const protectedCount = (manifest.protectedByContract || []).length;
        const patchCount = (manifest.downstreamPatches || []).length;
        byId('mode-summary').textContent = mode === 'propagate'
          ? `Propagate mode changes downstream output contracts and patches all ${{patchCount}} confirmed dbt consumers.`
          : `Preserve mode patches the one-hop boundary and leaves ${{protectedCount}} transitive consumer${{protectedCount === 1 ? '' : 's'}} protected by the stable output name.`;
      }}

      function diffLineClass(line) {{
        if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@')) return 'header';
        if (line.startsWith('+')) return 'add';
        if (line.startsWith('-')) return 'remove';
        return '';
      }}

      function renderDiff(artifact) {{
        const wrap = byId('diff-wrap');
        wrap.replaceChildren();
        wrap.setAttribute('aria-labelledby', `artifact-tab-${{activeArtifact}}`);
        if (!artifact) {{ wrap.className = 'diff-wrap empty'; wrap.textContent = 'No automatic patch was generated for this campaign.'; return; }}
        wrap.className = 'diff-wrap';
        const toolbar = create('div', 'diff-toolbar');
        toolbar.append(create('span', 'diff-path', `${{artifact.repository}}/${{artifact.sourcePath}}`), create('span', 'diff-count', artifact.kind));
        const pre = create('pre', 'diff');
        artifact.diff.replace(/\\r\\n/g, '\\n').split('\\n').forEach((line) => {{
          const span = create('span', `diff-line ${{diffLineClass(line)}}`, line || ' ');
          pre.append(span);
        }});
        wrap.append(toolbar, pre);
      }}

      function renderArtifacts(campaign) {{
        const artifacts = campaign.artifacts || [];
        const list = byId('artifact-list');
        list.replaceChildren();
        artifacts.forEach((artifact, index) => {{
          const button = create('button', 'artifact-button');
          button.type = 'button';
          button.role = 'tab';
          button.id = `artifact-tab-${{index}}`;
          button.tabIndex = index === activeArtifact ? 0 : -1;
          button.setAttribute('aria-controls', 'diff-wrap');
          button.setAttribute('aria-selected', String(index === activeArtifact));
          const covered = patchNames(artifact).join(', ');
          const meta = artifact.repository + ' · ' + artifact.kind + (covered ? ' · ' + covered : '');
          button.append(create('span', 'artifact-title', artifact.sourcePath), create('span', 'artifact-meta', meta));
          button.addEventListener('click', () => {{ activeArtifact = index; renderArtifacts(campaign); }});
          button.addEventListener('keydown', (event) => {{
            if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) return;
            event.preventDefault();
            const direction = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : -1;
            activeArtifact = (activeArtifact + direction + artifacts.length) % artifacts.length;
            renderArtifacts(campaign);
            byId(`artifact-tab-${{activeArtifact}}`).focus();
          }});
          list.append(button);
        }});
        byId('artifact-total').textContent = `${{artifacts.length}} file${{artifacts.length === 1 ? '' : 's'}}`;
        renderDiff(artifacts[activeArtifact]);
      }}

      function renderEvidence(campaign) {{
        const manifest = campaign.manifest;
        const root = byId('evidence-list');
        root.replaceChildren();
        const fingerprint = manifest.dataHubContext?.fingerprint;
        if (fingerprint?.sha256) {{
          const item = create('li', 'evidence-item');
          item.append(
            create('span', 'evidence-tool', 'context fingerprint'),
            create('span', 'evidence-detail', `SHA-256 ${{fingerprint.sha256}} · mutation boundaries require a repeated MCP match plus cache-bypassed lineage`),
          );
          root.append(item);
        }}
        const observations = campaign.contextVerification?.observations || {{}};
        for (const scenario of ['matched', 'refused-drift', 'refused-lineage-drift']) {{
          const observation = observations[scenario];
          if (!observation) continue;
          const item = create('li', 'evidence-item');
          const labels = {{
            matched: 'cache-bypassed context match',
            'refused-drift': 'owner drift refusal',
            'refused-lineage-drift': 'lineage drift refusal',
          }};
          const label = labels[scenario];
          const freshness = observation.lineageFreshness;
          const cacheDetail = freshness?.skipCache
            ? ` · lineage cache bypassed (${{freshness.matched ? 'matched MCP' : 'exposed mismatch'}})`
            : '';
          const result = scenario === 'matched'
            ? `matched at ${{observation.verifiedAt}}`
            : `${{observation.differences.join('; ')}} · refused at ${{observation.verifiedAt}}`;
          item.append(
            create('span', 'evidence-tool', label),
            create('span', 'evidence-detail', `${{result}} · observed SHA-256 ${{observation.freshSha256}}${{cacheDetail}}`),
          );
          root.append(item);
        }}
        const reads = manifest.dataHubContext?.reads || [];
        for (const read of reads) {{
          const item = create('li', 'evidence-item');
          item.append(create('span', 'evidence-tool', read.tool), create('span', 'evidence-detail', read.detail));
          root.append(item);
        }}
        if (!reads.length) root.append(create('li', 'evidence-item', 'This bundle does not record its DataHub read summary.'));
      }}

      function renderReviewRouting(manifest) {{
        const assignments = manifest.reviewAssignments || [];
        const root = byId('routing-list');
        root.replaceChildren();
        const assigned = assignments.filter((item) => item.routingStatus === 'assigned').length;
        byId('routing-summary').textContent = assignments.length
          ? `${{assigned}} of ${{assignments.length}} routes assigned from DataHub ownership`
          : 'Ownership routing was not recorded for this campaign.';
        for (const assignment of assignments) {{
          const item = create('div', 'routing-item');
          item.dataset.status = assignment.routingStatus;
          const reviewers = (assignment.reviewers || []).map((owner) => owner.name || owner.urn);
          item.append(
            create('strong', '', `${{assignment.assetName}} · ${{assignment.disposition}}`),
            create('span', 'evidence-detail', reviewers.length ? `Reviewers: ${{reviewers.join(', ')}}` : 'Reviewers: unassigned in DataHub'),
            create('span', 'evidence-detail', assignment.reason),
          );
          root.append(item);
        }}
      }}

      function renderModelMappings(manifest) {{
        const resolutions = manifest.modelMappingResolutions || [];
        const root = byId('mapping-list');
        root.replaceChildren();
        if (!resolutions.length) {{
          root.append(create('p', 'muted', 'No reviewer mapping override is recorded for this campaign.'));
          return;
        }}
        for (const resolution of resolutions) {{
          const item = create('div', 'routing-item');
          item.dataset.status = 'assigned';
          const candidates = resolution.candidates || [];
          // Compatibility with bundles created before unselectedCandidates was recorded.
          const unselected = Array.isArray(resolution.unselectedCandidates)
            ? resolution.unselectedCandidates
            : candidates.filter((candidate) => candidate !== resolution.selectedCandidate);
          item.append(
            create('strong', '', resolution.assetName),
            create('span', 'evidence-detail', `Selected: ${{resolution.selectedCandidate}}`),
            create('span', 'evidence-detail', unselected.length ? `Not selected: ${{unselected.join(', ')}}` : 'No alternate candidate remained.'),
            create('code', 'evidence-detail', `Resolution SHA-256: ${{resolution.resolutionSha256}}`),
          );
          root.append(item);
        }}
      }}

      function renderValidations(manifest) {{
        const root = byId('validation-list');
        root.replaceChildren();
        for (const check of manifest.validations || []) {{
          const item = create('li', 'validation-item');
          item.dataset.status = check.status;
          const name = create('span', 'validation-name', check.id.replaceAll('-', ' '));
          name.append(document.createTextNode(' '), create('span', 'validation-status', check.status));
          item.append(name, create('span', 'validation-detail', check.detail));
          root.append(item);
        }}
      }}

      function renderExecution(manifest) {{
        const validation = manifest.dbtValidation || {{ status: 'not-requested', projects: [] }};
        const summary = byId('execution-summary');
        const list = byId('execution-list');
        list.replaceChildren();
        if (validation.status === 'not-requested') {{
          summary.textContent = 'Not run for this captured campaign.';
          return;
        }}
        summary.textContent = `${{validation.status.toUpperCase()}} · dbt ${{validation.dbtVersion}} · ${{validation.adapter}}`;
        for (const project of validation.projects || []) {{
          const item = create('div', 'execution-project');
          item.dataset.status = project.status;
          item.append(create('strong', '', `${{project.repository}} · ${{project.status}}`));
          for (const step of project.steps || []) {{
            const row = create('div', 'execution-step');
            row.append(
              create('span', 'evidence-detail', `${{step.name}}: ${{step.summary}}`),
              create('code', 'execution-command', step.command.join(' ')),
            );
            item.append(row);
          }}
          list.append(item);
        }}
      }}

      function renderPostgresValidation(campaign) {{
        const evidence = campaign.postgresValidationEvidence;
        const validation = evidence?.validation || campaign.manifest.postgresValidation || {{ status: 'not-requested', steps: [] }};
        const summary = byId('postgres-summary');
        const list = byId('postgres-list');
        list.replaceChildren();
        if (validation.status === 'not-requested') {{
          summary.textContent = 'Not run for this captured campaign.';
          return;
        }}
        summary.textContent = `${{validation.status.toUpperCase()}} · PostgreSQL ${{validation.engineVersion}} · ${{validation.image}}`;
        const observations = create('div', 'execution-project');
        observations.dataset.status = validation.status;
        observations.append(
          create('strong', '', 'Schema observations'),
          create('span', 'evidence-detail', `Before: ${{(validation.beforeColumns || []).join(', ') || 'not observed'}}`),
          create('span', 'evidence-detail', `After rename: ${{(validation.afterColumns || []).join(', ') || 'not observed'}}`),
          create('span', 'evidence-detail', `After rollback: ${{(validation.rollbackColumns || []).join(', ') || 'not observed'}}`),
          create('span', 'evidence-detail', `Validated at: ${{validation.validatedAt}} · ${{validation.isolation}}`),
          create('code', 'evidence-detail', `Migration SHA-256: ${{validation.migrationSha256}}`),
        );
        list.append(observations);
        for (const step of validation.steps || []) {{
          const item = create('div', 'execution-project');
          item.dataset.status = step.status;
          item.append(
            create('strong', '', `${{step.name}} · ${{step.status}}`),
            create('span', 'evidence-detail', step.summary),
            create('code', 'execution-command', (step.command || []).join(' ')),
          );
          list.append(item);
        }}
      }}

      function renderDelivery(campaign) {{
        const publication = campaign.publication;
        const summary = byId('delivery-summary');
        const list = byId('delivery-list');
        list.replaceChildren();
        if (!publication) {{
          summary.textContent = 'Not published from this captured campaign.';
          return;
        }}
        const repositories = publication.repositories || [];
        summary.textContent = `${{publication.status.toUpperCase()}} · ${{repositories.length}} pull request${{repositories.length === 1 ? '' : 's'}} · auto-merge disabled`;
        for (const repository of repositories) {{
          const item = create('div', 'delivery-repo');
          item.dataset.status = repository.checks?.status || 'pending';
          item.append(create('strong', '', repository.repository));
          item.append(create('span', 'evidence-detail', `Checks: ${{repository.checks?.status || 'pending'}} · branch: ${{repository.headBranch}}`));
          const url = repository.pullRequest?.url;
          if (typeof url === 'string' && url.startsWith('https://github.com/')) {{
            const link = create('a', 'delivery-link', `Open pull request #${{repository.pullRequest.number}}`);
            link.href = url;
            link.target = '_blank';
            link.rel = 'noreferrer';
            item.append(link);
          }}
          list.append(item);
        }}
      }}

      function renderDecision(campaign) {{
        const decision = campaign.decision;
        const root = byId('decision-body');
        root.replaceChildren();
        if (!decision) {{
          root.append(create('p', 'evidence-detail', 'No structured DataHub review decision is stored for this captured campaign.'));
          return;
        }}
        const card = create('div', 'decision-card');
        card.append(create('span', 'decision-status', decision.decision));
        card.append(create('span', 'evidence-detail', `${{decision.verifiedAssets.length}} of ${{decision.assets.length}} assets independently read back · merge remains ${{decision.mergePolicy}}`));
        const rows = create('div', 'decision-grid');
        const sourceChange = decision.properties?.['urn:li:structuredProperty:io.github.itxcrusher.ripple_proof.sourceChange']?.[0];
        // Compatibility with evidence created before the RippleProof property namespace.
        const legacySourceChange = decision.properties?.['urn:li:structuredProperty:io.github.itxcrusher.blastradius.sourceChange']?.[0];
        const values = [
          ['Source change', sourceChange || legacySourceChange || 'Not recorded'],
          ['Manifest SHA-256', decision.campaignManifestSha256],
          ['Recorded at', decision.approvedAt],
          ['Readback', decision.freshSessionReadback ? 'Passed in a fresh DataHub client session' : 'Not verified'],
        ];
        for (const [label, value] of values) {{
          const row = create('div', 'decision-row');
          row.append(create('span', 'decision-label', label), create('span', 'decision-value', value));
          rows.append(row);
        }}
        card.append(rows);
        root.append(card);
      }}

      function renderSafety(campaign) {{
        const manifest = campaign.manifest;
        const safety = manifest.safety || {{}};
        const postgresValidation = campaign.postgresValidationEvidence?.validation || manifest.postgresValidation;
        const rows = [
          ['Source SQL execution', safety.sqlExecuted ? 'Executed' : 'Not executed'],
          ['Target repositories', safety.targetRepositoriesModified ? 'Modified' : 'Not modified'],
          ['Human review', safety.humanReviewRequired ? 'Required before merge' : 'Not required'],
          ['Output contract', safety.downstreamOutputContractMayChange ? 'May change in this mode' : 'Preserved at the boundary'],
          ['dbt validation', safety.dbtValidationExecuted ? 'Executed in copied workspaces' : 'Not requested'],
          ['PostgreSQL validation', postgresValidation?.status === 'passed' ? 'Passed in a disposable rolled-back database' : (postgresValidation?.status === 'failed' ? 'Failed in the disposable database' : 'Not requested')],
        ];
        const root = byId('safety-list');
        root.replaceChildren();
        for (const [label, detail] of rows) {{
          const item = create('li', 'safety-item');
          item.append(create('strong', '', label), create('span', 'safety-detail', detail));
          root.append(item);
        }}
      }}

      function render() {{
        const campaign = campaigns[activeCampaign];
        const manifest = campaign.manifest;
        const change = rename(manifest);
        renderTabs();
        byId('status').textContent = manifest.readyForReview ? 'Ready for review' : 'Blocked by validation';
        byId('status').style.background = manifest.readyForReview ? 'var(--success-soft)' : 'var(--danger-soft)';
        byId('status').style.color = manifest.readyForReview ? 'var(--success)' : 'var(--danger)';
        byId('context-source').textContent = manifest.dataHubContext?.capture === 'live-mcp' ? 'Captured from real DataHub MCP reads' : 'Recorded campaign evidence';
        byId('campaign-title').textContent = `${{sourceName(manifest)}} column rename`;
        byId('lede').textContent = manifest.downstreamColumnMode === 'propagate'
          ? 'Propagate the new field through every confirmed dbt consumer and update declared contracts.'
          : 'Change the source while keeping the confirmed one-hop downstream output stable.';
        byId('old-column').textContent = `- ${{change.oldColumn}}`;
        byId('new-column').textContent = `+ ${{change.newColumn}}`;
        renderMetrics(campaign);
        renderAudit(campaign);
        renderLineage(campaign);
        renderArtifacts(campaign);
        renderEvidence(campaign);
        renderReviewRouting(manifest);
        renderModelMappings(manifest);
        renderValidations(manifest);
        renderExecution(manifest);
        renderPostgresValidation(campaign);
        renderDecision(campaign);
        renderDelivery(campaign);
        renderSafety(campaign);
        byId('migration').textContent = campaign.migrationSql;
        byId('impact-report').textContent = campaign.impactReport;
      }}

      render();
    }})();
  </script>
</body>
</html>
"""


def write_replay(
    campaigns: Sequence[dict[str, Any]],
    output_file: str | Path,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically write a self-contained campaign review page."""
    destination = Path(output_file).expanduser().resolve()
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Replay output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    html = render_replay_html(campaigns)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(html)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render one or more RippleProof bundles as a self-contained review page."
    )
    parser.add_argument(
        "--bundle",
        action="append",
        required=True,
        help="bundle directory; repeat to compare campaign modes",
    )
    parser.add_argument("--output", required=True, help="HTML file to create")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    campaigns = [load_campaign(bundle) for bundle in args.bundle]
    output = write_replay(campaigns, args.output, overwrite=args.force)
    print(f"Campaign review: {output}")


if __name__ == "__main__":
    main()
