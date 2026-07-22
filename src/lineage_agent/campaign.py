"""Plan and package validated, lineage-aware repair campaigns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .bundling import ModelPatch, UnresolvedAsset, plan_model_patches
from .contracts import ContractPatch, plan_contract_patches
from .dbt_validation import DbtValidation, validate_dbt_campaign
from .replay import load_campaign, render_replay_html
from .reporting import render_markdown_report


DATASET_URN_RE = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^)]+)\)$"
)
PROJECT_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]+$")
AMBIGUOUS_MODEL_PREFIX = "Multiple dbt models match this DataHub asset:"


@dataclass(frozen=True)
class ProtectedAsset:
    asset_urn: str
    asset_name: str
    reason: str
    boundary_assets: tuple[str, ...]


@dataclass(frozen=True)
class ValidationCheck:
    check_id: str
    status: str
    detail: str


@dataclass(frozen=True)
class ModelMappingResolution:
    asset_urn: str
    asset_name: str
    selected_candidate: str
    selected_source_sha256: str
    candidates: tuple[str, ...]

    def to_manifest(self) -> dict[str, Any]:
        decision = {
            "assetUrn": self.asset_urn,
            "assetName": self.asset_name,
            "selectedCandidate": self.selected_candidate,
            "selectedSourceSha256": self.selected_source_sha256,
            "candidates": list(self.candidates),
            "unselectedCandidates": [
                candidate
                for candidate in self.candidates
                if candidate != self.selected_candidate
            ],
            "resolutionSource": "reviewer-override",
        }
        decision["resolutionSha256"] = _sha256(
            json.dumps(decision, sort_keys=True, separators=(",", ":"))
        )
        return decision


@dataclass(frozen=True)
class ChangeBundle:
    output_dir: Path
    patches: tuple[ModelPatch, ...]
    contract_patches: tuple[ContractPatch, ...]
    protected: tuple[ProtectedAsset, ...]
    unresolved: tuple[UnresolvedAsset, ...]
    validations: tuple[ValidationCheck, ...]
    artifact_summary: str
    downstream_mode: str
    ready_for_review: bool
    dbt_validation: DbtValidation | None
    model_mapping_resolutions: tuple[ModelMappingResolution, ...] = ()


@dataclass(frozen=True)
class _ModelCandidate:
    repository: str
    root: Path
    path: Path
    discovery: str


@dataclass(frozen=True)
class _DbtProject:
    name: str
    root: Path
    manifest_models: dict[str, tuple[Path, ...]]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def parse_model_maps(values: Sequence[str]) -> dict[str, str]:
    """Parse exact DataHub-URN to discovered dbt-model selections."""
    mappings: dict[str, str] = {}
    for value in values:
        asset_urn, separator, candidate = value.partition("=")
        asset_urn = asset_urn.strip()
        candidate = candidate.strip().replace("\\", "/")
        if not separator or not asset_urn or not candidate:
            raise ValueError(
                "Model mappings must use DATAHUB_URN=repository/models/model.sql"
            )
        if asset_urn in mappings:
            raise ValueError(f"Duplicate model mapping for DataHub asset: {asset_urn}")
        mappings[asset_urn] = candidate
    return mappings


def _asset_model_names(asset: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    raw_name = str(asset.get("name") or "").strip()
    if raw_name:
        names.extend((raw_name, raw_name.replace("\\", "/").rsplit("/", 1)[-1]))
        names.append(raw_name.rsplit(".", 1)[-1])
    match = DATASET_URN_RE.match(str(asset.get("urn") or ""))
    if match:
        dataset_name = match.group(2)
        names.extend((dataset_name, dataset_name.rsplit(".", 1)[-1]))
    return tuple(dict.fromkeys(name.casefold() for name in names if name))


def _relation_leaf(value: str) -> str:
    leaf = value.rsplit(".", 1)[-1].strip()
    return leaf.strip('"`[]')


def _load_manifest_models(root: Path) -> dict[str, tuple[Path, ...]]:
    manifest_path = root / "target" / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read dbt manifest {manifest_path}: {exc}") from exc

    indexed: dict[str, list[Path]] = {}
    for unique_id, node in (manifest.get("nodes") or {}).items():
        if not isinstance(node, dict) or node.get("resource_type") != "model":
            continue
        original_path = node.get("original_file_path")
        if not isinstance(original_path, str):
            continue
        candidate = (root / original_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError(
                f"dbt manifest model path escapes target repository: {candidate}"
            ) from None
        if not candidate.is_file() or candidate.suffix.casefold() != ".sql":
            continue

        identifiers = {
            str(node.get("name") or ""),
            str(node.get("alias") or ""),
            str(node.get("identifier") or ""),
            str(unique_id).rsplit(".", 1)[-1],
            _relation_leaf(str(node.get("relation_name") or "")),
        }
        for identifier in identifiers:
            if identifier:
                indexed.setdefault(identifier.casefold(), []).append(candidate)

    return {
        identifier: tuple(dict.fromkeys(paths))
        for identifier, paths in indexed.items()
    }


def _normalize_projects(
    target_repos: str | Path | Sequence[str | Path],
) -> tuple[_DbtProject, ...]:
    if isinstance(target_repos, (str, Path)):
        values = (target_repos,)
    else:
        values = tuple(target_repos)
    if not values:
        raise ValueError("At least one target dbt repository is required")

    projects: list[_DbtProject] = []
    labels: set[str] = set()
    roots: set[Path] = set()
    for value in values:
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Target repository does not exist: {root}")
        if root in roots:
            raise ValueError(f"Target repository was provided more than once: {root}")
        name = root.name
        if not PROJECT_LABEL_RE.fullmatch(name):
            raise ValueError(
                f"Target repository directory must have a portable name: {name!r}"
            )
        if name.casefold() in labels:
            raise ValueError(
                f"Target repository labels must be unique; duplicate directory name: {name}"
            )
        roots.add(root)
        labels.add(name.casefold())
        projects.append(
            _DbtProject(
                name=name,
                root=root,
                manifest_models=_load_manifest_models(root),
            )
        )
    return tuple(projects)


def _find_model_candidates(
    projects: tuple[_DbtProject, ...],
    asset: dict[str, Any],
) -> tuple[_ModelCandidate, ...]:
    asset_names = _asset_model_names(asset)
    manifest_candidates: list[_ModelCandidate] = []
    for project in projects:
        paths: list[Path] = []
        for name in asset_names:
            paths.extend(project.manifest_models.get(name, ()))
        for path in dict.fromkeys(paths):
            manifest_candidates.append(
                _ModelCandidate(project.name, project.root, path, "dbt-manifest")
            )
    if manifest_candidates:
        return tuple(manifest_candidates)

    filename_candidates: list[_ModelCandidate] = []
    for project in projects:
        models_root = project.root / "models"
        if not models_root.is_dir():
            continue
        for path in sorted(models_root.rglob("*.sql")):
            if path.stem.casefold() in asset_names:
                filename_candidates.append(
                    _ModelCandidate(project.name, project.root, path, "filename")
                )
    return tuple(filename_candidates)


def _candidate_label(candidate: _ModelCandidate) -> str:
    relative = candidate.path.relative_to(candidate.root).as_posix()
    return f"{candidate.repository}/{relative}"


def _resolve_model_overrides(
    projects: tuple[_DbtProject, ...],
    planned_assets: Sequence[dict[str, Any]],
    model_maps: Mapping[str, str],
) -> tuple[
    dict[str, _ModelCandidate],
    tuple[ModelMappingResolution, ...],
]:
    assets_by_urn = {
        str(asset.get("urn") or "unknown"): asset for asset in planned_assets
    }
    unknown = sorted(set(model_maps) - set(assets_by_urn))
    if unknown:
        raise ValueError(
            "Model mapping does not target a patch-eligible DataHub asset in this "
            f"campaign: {', '.join(unknown)}"
        )

    selected: dict[str, _ModelCandidate] = {}
    resolutions: list[ModelMappingResolution] = []
    for asset_urn, requested in model_maps.items():
        asset = assets_by_urn[asset_urn]
        candidates = _find_model_candidates(projects, asset)
        labels = tuple(_candidate_label(candidate) for candidate in candidates)
        if len(candidates) < 2:
            raise ValueError(
                f"Model mapping overrides are only valid for ambiguous assets; "
                f"{asset_urn} has {len(candidates)} discovered candidate(s)."
            )
        matches = [
            candidate
            for candidate in candidates
            if _candidate_label(candidate) == requested
        ]
        if len(matches) != 1:
            choices = ", ".join(labels)
            raise ValueError(
                f"Model mapping for {asset_urn} must select exactly one discovered "
                f"candidate. Available candidates: {choices}."
            )
        selected[asset_urn] = matches[0]
        resolutions.append(
            ModelMappingResolution(
                asset_urn=asset_urn,
                asset_name=str(asset.get("name") or asset_urn),
                selected_candidate=requested,
                selected_source_sha256=_sha256(
                    matches[0].path.read_text(encoding="utf-8")
                ),
                candidates=labels,
            )
        )
    return selected, tuple(resolutions)


def _is_confirmed_dbt_dataset(asset: dict[str, Any]) -> bool:
    return (
        asset.get("evidence") == "column"
        and str(asset.get("type") or "").upper() == "DATASET"
        and str(asset.get("platform") or "").casefold() == "dbt"
    )


def _plan_asset(
    projects: tuple[_DbtProject, ...],
    asset: dict[str, Any],
    old_column: str,
    new_column: str,
    downstream_mode: str,
    candidate_override: _ModelCandidate | None = None,
) -> tuple[ModelPatch | None, UnresolvedAsset | None]:
    asset_urn = str(asset.get("urn") or "unknown")
    asset_name = str(asset.get("name") or asset_urn)
    candidates = _find_model_candidates(projects, asset)
    if not candidates:
        return None, UnresolvedAsset(
            asset_urn,
            asset_name,
            "No matching dbt model was found across the supplied repositories.",
        )
    if len(candidates) > 1 and candidate_override is None:
        choices = ", ".join(_candidate_label(candidate) for candidate in candidates)
        return None, UnresolvedAsset(
            asset_urn,
            asset_name,
            f"{AMBIGUOUS_MODEL_PREFIX} {choices}.",
        )

    candidate = candidate_override or candidates[0]
    patches, unresolved = plan_model_patches(
        candidate.root,
        (asset,),
        old_column,
        new_column,
        downstream_mode,
        model_path_overrides={asset_urn: candidate.path},
    )
    if unresolved:
        return None, unresolved[0]
    if len(patches) != 1:
        return None, UnresolvedAsset(
            asset_urn,
            asset_name,
            "The dbt model did not produce exactly one reviewable patch.",
        )
    return (
        replace(
            patches[0],
            repository=candidate.repository,
            discovery=candidate.discovery,
        ),
        None,
    )


def _plan_repair_campaign(
    target_repos: str | Path | Sequence[str | Path],
    downstream: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    old_column: str,
    new_column: str,
    downstream_mode: str = "preserve",
    model_maps: Mapping[str, str] | None = None,
) -> tuple[
    tuple[_DbtProject, ...],
    tuple[ModelPatch, ...],
    tuple[ProtectedAsset, ...],
    tuple[UnresolvedAsset, ...],
    tuple[ModelMappingResolution, ...],
]:
    """Plan a contract-aware repair campaign across one or more dbt projects."""
    if downstream_mode not in {"preserve", "propagate"}:
        raise ValueError(f"Unsupported downstream column mode: {downstream_mode}")
    projects = _normalize_projects(target_repos)
    patches: list[ModelPatch] = []
    protected: list[ProtectedAsset] = []
    unresolved: list[UnresolvedAsset] = []

    code_assets: list[dict[str, Any]] = []
    for asset in downstream:
        asset_urn = str(asset.get("urn") or "unknown")
        asset_name = str(asset.get("name") or asset_urn)
        if not _is_confirmed_dbt_dataset(asset):
            reason = (
                "DataHub reports asset-level reachability but not column usage."
                if asset.get("evidence") != "column"
                else "Automatic patches support confirmed dbt datasets only."
            )
            unresolved.append(UnresolvedAsset(asset_urn, asset_name, reason))
            continue
        code_assets.append(asset)

    if downstream_mode == "propagate":
        planned_assets = code_assets
        transitive_assets: list[dict[str, Any]] = []
    else:
        planned_assets = [asset for asset in code_assets if asset.get("degree") == 1]
        transitive_assets = [
            asset
            for asset in code_assets
            if isinstance(asset.get("degree"), int) and asset["degree"] > 1
        ]
        unknown_hop_assets = [
            asset
            for asset in code_assets
            if not isinstance(asset.get("degree"), int) or asset["degree"] < 1
        ]
        for asset in unknown_hop_assets:
            asset_urn = str(asset.get("urn") or "unknown")
            unresolved.append(
                UnresolvedAsset(
                    asset_urn,
                    str(asset.get("name") or asset_urn),
                    "DataHub did not provide a valid downstream hop distance, so "
                    "BlastRadius cannot prove where to place a compatibility boundary.",
                )
            )

    selected_candidates, model_mapping_resolutions = _resolve_model_overrides(
        projects,
        planned_assets,
        model_maps or {},
    )

    for asset in planned_assets:
        asset_urn = str(asset.get("urn") or "unknown")
        patch, failure = _plan_asset(
            projects,
            asset,
            old_column,
            new_column,
            downstream_mode,
            selected_candidates.get(asset_urn),
        )
        if patch is not None:
            patches.append(patch)
        if failure is not None:
            unresolved.append(failure)

    if downstream_mode == "preserve" and transitive_assets:
        direct_asset_urns = {
            str(asset.get("urn")) for asset in planned_assets
        }
        patched_direct_urns = {patch.asset_urn for patch in patches}
        boundaries = tuple(
            patch.asset_urn
            for patch in patches
            if patch.preserved_output_aliases > 0
        )
        boundary_complete = (
            bool(direct_asset_urns)
            and patched_direct_urns == direct_asset_urns
            and len(boundaries) == len(direct_asset_urns)
        )
        for asset in transitive_assets:
            asset_urn = str(asset.get("urn") or "unknown")
            asset_name = str(asset.get("name") or asset_urn)
            if boundary_complete:
                protected.append(
                    ProtectedAsset(
                        asset_urn=asset_urn,
                        asset_name=asset_name,
                        reason=(
                            f"No edit is required while the one-hop dbt boundary keeps "
                            f"the `{old_column}` output contract."
                        ),
                        boundary_assets=boundaries,
                    )
                )
            else:
                unresolved.append(
                    UnresolvedAsset(
                        asset_urn,
                        asset_name,
                        "A transitive consumer cannot be considered protected because "
                        "not every one-hop dbt boundary produced a preserved output alias.",
                    )
                )

    return (
        projects,
        tuple(patches),
        tuple(protected),
        tuple(unresolved),
        model_mapping_resolutions,
    )


def plan_repair_campaign(
    target_repos: str | Path | Sequence[str | Path],
    downstream: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    old_column: str,
    new_column: str,
    downstream_mode: str = "preserve",
    model_maps: Mapping[str, str] | None = None,
) -> tuple[
    tuple[_DbtProject, ...],
    tuple[ModelPatch, ...],
    tuple[ProtectedAsset, ...],
    tuple[UnresolvedAsset, ...],
]:
    """Plan a campaign while preserving the established four-value API."""
    projects, patches, protected, unresolved, _resolutions = _plan_repair_campaign(
        target_repos,
        downstream,
        old_column,
        new_column,
        downstream_mode,
        model_maps,
    )
    return projects, patches, protected, unresolved


def _validate_campaign(
    downstream: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    patches: tuple[ModelPatch, ...],
    contract_patches: tuple[ContractPatch, ...],
    protected: tuple[ProtectedAsset, ...],
    unresolved: tuple[UnresolvedAsset, ...],
    downstream_mode: str,
    dbt_validation: DbtValidation | None = None,
    source_owners: tuple[dict[str, str], ...] = (),
    model_mapping_resolutions: tuple[ModelMappingResolution, ...] = (),
) -> tuple[ValidationCheck, ...]:
    model_integrity_failures = [
        patch
        for patch in patches
        if not patch.unified_diff
        or patch.original_sha256 != _sha256(patch.original_sql)
        or patch.proposed_sha256 != _sha256(patch.proposed_sql)
        or patch.original_sha256
        != _sha256(patch.source_path.read_text(encoding="utf-8"))
    ]
    contract_integrity_failures = [
        patch
        for patch in contract_patches
        if not patch.unified_diff
        or patch.original_sha256 != _sha256(patch.original_yaml)
        or patch.proposed_sha256 != _sha256(patch.proposed_yaml)
        or patch.original_sha256
        != _sha256(patch.source_path.read_text(encoding="utf-8"))
    ]
    integrity_failures = model_integrity_failures + contract_integrity_failures
    integrity = ValidationCheck(
        "patch-integrity",
        "failed" if integrity_failures else "passed",
        (
            "Every generated SQL and dbt contract diff is non-empty and its "
            "source/proposed hashes match."
            if not integrity_failures
            else f"{len(integrity_failures)} generated patch(es) failed hash validation."
        ),
    )

    if downstream_mode == "preserve":
        invalid_contracts = [
            patch
            for patch in patches
            if patch.preserved_output_aliases != 1
            or patch.remaining_executable_old_identifiers != 1
        ]
        contract_detail = (
            "Every edited one-hop model preserves exactly one downstream output alias; "
            "transitive models are left unchanged."
            if not invalid_contracts
            else f"{len(invalid_contracts)} patch(es) violate the preserve-mode contract."
        )
    else:
        invalid_contracts = [
            patch
            for patch in patches
            if patch.preserved_output_aliases != 0
            or patch.remaining_executable_old_identifiers != 0
        ]
        patched_contract_assets = {patch.asset_urn for patch in contract_patches}
        contract_detail = (
            "Every patched model propagates the new output name without a legacy "
            f"alias; {len(patched_contract_assets)} declared dbt contract(s) were "
            "updated with it."
            if not invalid_contracts
            else f"{len(invalid_contracts)} patch(es) retain an old executable identifier."
        )
    contracts = ValidationCheck(
        "output-contract",
        "failed" if invalid_contracts else "passed",
        contract_detail,
    )

    expected = {str(asset.get("urn") or "unknown") for asset in downstream}
    accounted = (
        {patch.asset_urn for patch in patches}
        | {asset.asset_urn for asset in protected}
        | {asset.asset_urn for asset in unresolved}
    )
    missing = expected - accounted
    accounting = ValidationCheck(
        "lineage-accounting",
        "failed" if missing else "passed",
        (
            f"All {len(expected)} DataHub lineage asset(s) are patched, protected, or "
            "assigned to manual review."
            if not missing
            else f"{len(missing)} DataHub lineage asset(s) have no disposition."
        ),
    )

    review = ValidationCheck(
        "manual-review",
        "warning" if unresolved else "passed",
        (
            f"{len(unresolved)} asset(s) still require human investigation."
            if unresolved
            else "No lineage assets remain unresolved."
        ),
    )
    ownership_recorded = bool(downstream) and all(
        "owners" in asset for asset in downstream
    )
    unassigned = sum(not (asset.get("owners") or ()) for asset in downstream)
    if not source_owners:
        unassigned += 1
    routed = ValidationCheck(
        "review-routing",
        "passed" if ownership_recorded and unassigned == 0 else "warning",
        (
            f"DataHub ownership assigns reviewers to the source and all "
            f"{len(downstream)} downstream asset(s)."
            if ownership_recorded and unassigned == 0
            else (
                f"DataHub ownership leaves {unassigned} source or downstream "
                "asset(s) without an assigned reviewer."
                if ownership_recorded
                else "DataHub ownership lookup was not recorded for this campaign."
            )
        ),
    )
    ambiguous_mappings = [
        asset
        for asset in unresolved
        if asset.reason.startswith(AMBIGUOUS_MODEL_PREFIX)
    ]
    mapping_resolution = ValidationCheck(
        "model-mapping",
        "failed" if ambiguous_mappings else "passed",
        (
            f"{len(ambiguous_mappings)} DataHub asset(s) still map to multiple dbt "
            "models; an explicit reviewer override is required."
            if ambiguous_mappings
            else (
                f"{len(model_mapping_resolutions)} ambiguous DataHub asset mapping(s) "
                "were explicitly resolved and hash-recorded."
                if model_mapping_resolutions
                else "No ambiguous DataHub-to-dbt model mapping remains."
            )
        ),
    )
    checks = [integrity, contracts, accounting, review, routed, mapping_resolution]
    if dbt_validation is not None:
        validated_projects = len(dbt_validation.projects)
        checks.append(
            ValidationCheck(
                "dbt-build",
                "passed" if dbt_validation.status == "passed" else "failed",
                (
                    f"dbt {dbt_validation.dbt_version} built and tested the proposed "
                    f"campaign with DuckDB across {validated_projects} validation root(s)."
                    if dbt_validation.status == "passed"
                    else "The isolated dbt build failed; inspect executable validation evidence."
                ),
            )
        )
    return tuple(checks)


def _review_assignments(
    *,
    source_urn: str,
    source_name: str,
    source_owners: tuple[dict[str, str], ...],
    downstream: Sequence[dict[str, Any]],
    patches: tuple[ModelPatch, ...],
    protected: tuple[ProtectedAsset, ...],
    unresolved: tuple[UnresolvedAsset, ...],
) -> list[dict[str, Any]]:
    patch_by_urn = {patch.asset_urn: patch for patch in patches}
    protected_by_urn = {asset.asset_urn: asset for asset in protected}
    unresolved_by_urn = {asset.asset_urn: asset for asset in unresolved}
    assignments = [
        {
            "assetUrn": source_urn,
            "assetName": source_name,
            "disposition": "source-change",
            "reason": "Review the proposed source migration before execution.",
            "reviewers": list(source_owners),
            "routingStatus": "assigned" if source_owners else "unassigned",
        }
    ]
    for asset in downstream:
        urn = str(asset.get("urn") or "unknown")
        if urn in unresolved_by_urn:
            disposition = "manual-review"
            reason = unresolved_by_urn[urn].reason
        elif urn in protected_by_urn:
            disposition = "protected"
            reason = protected_by_urn[urn].reason
        elif urn in patch_by_urn:
            disposition = "patched"
            patch = patch_by_urn[urn]
            reason = (
                f"Review generated changes in "
                f"{patch.repository}/{patch.relative_path.as_posix()}."
            )
        else:
            disposition = "manual-review"
            reason = "No campaign disposition was recorded for this asset."
        owners = tuple(asset.get("owners") or ())
        assignments.append(
            {
                "assetUrn": urn,
                "assetName": str(asset.get("name") or urn),
                "disposition": disposition,
                "reason": reason,
                "reviewers": list(owners),
                "routingStatus": "assigned" if owners else "unassigned",
            }
        )
    return assignments


def _artifact_summary(
    patches: tuple[ModelPatch, ...],
    contract_patches: tuple[ContractPatch, ...],
    protected: tuple[ProtectedAsset, ...],
    unresolved: tuple[UnresolvedAsset, ...],
    model_mapping_resolutions: tuple[ModelMappingResolution, ...],
    validations: tuple[ValidationCheck, ...],
    downstream_mode: str,
    dbt_validation: DbtValidation | None = None,
) -> str:
    lines: list[str] = []
    if patches:
        lines.append("Proposed downstream patches:")
        for patch in patches:
            lines.append(
                f"- `{patch.repository}/{patch.relative_path.as_posix()}` for "
                f"`{patch.asset_name}` ({patch.replacement_count} executable "
                f"reference(s) updated, {patch.preserved_output_aliases} output "
                f"alias(es) preserved; discovered via {patch.discovery})"
            )
    else:
        lines.append("No downstream code patch could be generated automatically.")
    if contract_patches:
        lines.append("\nUpdated dbt schema contracts:")
        for patch in contract_patches:
            lines.append(
                f"- `{patch.repository}/{patch.relative_path.as_posix()}` for "
                f"`{patch.asset_name}`"
            )
    if protected:
        lines.append("\nConsumers protected by an unchanged output contract:")
        for asset in protected:
            lines.append(f"- `{asset.asset_name}`: {asset.reason}")
    if unresolved:
        lines.append("\nAssets requiring manual review:")
        for asset in unresolved:
            lines.append(f"- `{asset.asset_name}`: {asset.reason}")
    if model_mapping_resolutions:
        lines.append("\nExplicit model mapping resolutions:")
        for resolution in model_mapping_resolutions:
            unselected = [
                candidate
                for candidate in resolution.candidates
                if candidate != resolution.selected_candidate
            ]
            lines.append(
                f"- `{resolution.asset_name}`: selected "
                f"`{resolution.selected_candidate}`; retained unselected candidate(s): "
                f"{', '.join(f'`{candidate}`' for candidate in unselected)}"
            )
    lines.append("\nValidation gates:")
    for check in validations:
        lines.append(f"- {check.status.upper()} `{check.check_id}`: {check.detail}")
    if dbt_validation is not None:
        lines.append(
            f"\nExecutable validation: {dbt_validation.status.upper()} with "
            f"dbt {dbt_validation.dbt_version} and DuckDB in disposable copied repositories."
        )
    if downstream_mode == "preserve":
        lines.append(
            "\nPatch mode: preserve the one-hop dbt output contract and avoid "
            "unnecessary transitive edits."
        )
    else:
        lines.append(
            "\nPatch mode: propagate the rename through every confirmed dbt consumer. "
            "This changes downstream output contracts."
        )
    lines.append(
        "The target repositories were not modified. Proposed files and diffs require human review."
    )
    return "\n".join(lines)


def _write_text(root: Path, relative: Path, content: str) -> None:
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="\n")


def build_change_bundle(
    *,
    result: Any,
    source_urn: str,
    instruction: str,
    target_repos: str | Path | Sequence[str | Path],
    output_dir: str | Path,
    downstream_mode: str = "preserve",
    model_maps: Mapping[str, str] | None = None,
    validate_dbt: bool = False,
    dbt_executable: str | Path = "dbt",
) -> ChangeBundle:
    """Atomically create a validated repair campaign without editing targets."""
    rename = getattr(result, "rename", None)
    if rename is None:
        raise ValueError("The generated change does not contain a resolved rename intent")

    (
        projects,
        patches,
        protected,
        unresolved,
        model_mapping_resolutions,
    ) = _plan_repair_campaign(
        target_repos,
        result.impact.downstream,
        rename.old_column,
        rename.new_column,
        downstream_mode,
        model_maps,
    )
    contract_patches, contract_unresolved = plan_contract_patches(
        {project.name: project.root for project in projects},
        patches,
        rename.old_column,
        rename.new_column,
        downstream_mode,
    )
    unresolved = unresolved + contract_unresolved
    destination = Path(output_dir).expanduser().resolve()
    for project in projects:
        try:
            destination.relative_to(project.root)
        except ValueError:
            continue
        raise ValueError(
            "Bundle output must be outside the target repository or repositories "
            "so source files remain untouched"
        )
    if destination.exists():
        raise FileExistsError(f"Bundle output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    dbt_validation = None
    if validate_dbt:
        proposed_files = [
            (patch.repository, patch.relative_path, patch.proposed_sql)
            for patch in patches
        ] + [
            (patch.repository, patch.relative_path, patch.proposed_yaml)
            for patch in contract_patches
        ]
        dbt_validation = validate_dbt_campaign(
            {project.name: project.root for project in projects},
            proposed_files,
            dbt_executable=dbt_executable,
        )

    validations = _validate_campaign(
        result.impact.downstream,
        patches,
        contract_patches,
        protected,
        unresolved,
        downstream_mode,
        dbt_validation,
        tuple(getattr(result, "source_owners", ())),
        model_mapping_resolutions,
    )
    ready_for_review = bool(patches) and not any(
        check.status == "failed" for check in validations
    )
    artifact_summary = _artifact_summary(
        patches,
        contract_patches,
        protected,
        unresolved,
        model_mapping_resolutions,
        validations,
        downstream_mode,
        dbt_validation,
    )

    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    multiple_projects = len(projects) > 1
    try:
        _write_text(temporary, Path("migration.sql"), f"{result.change_sql}\n")
        manifest_patches = []
        for patch in patches:
            prefix = Path(patch.repository) if multiple_projects else Path()
            artifact_path = prefix / patch.relative_path
            proposed_path = Path("proposed") / artifact_path
            diff_path = Path("patches") / Path(f"{artifact_path.as_posix()}.patch")
            _write_text(temporary, proposed_path, patch.proposed_sql)
            _write_text(temporary, diff_path, patch.unified_diff)
            manifest_patches.append(
                {
                    "assetUrn": patch.asset_urn,
                    "assetName": patch.asset_name,
                    "repository": patch.repository,
                    "discovery": patch.discovery,
                    "sourcePath": patch.relative_path.as_posix(),
                    "proposedPath": proposed_path.as_posix(),
                    "diffPath": diff_path.as_posix(),
                    "replacementCount": patch.replacement_count,
                    "preservedOutputAliases": patch.preserved_output_aliases,
                    "remainingExecutableOldIdentifiers": (
                        patch.remaining_executable_old_identifiers
                    ),
                    "sourceSha256": patch.original_sha256,
                    "proposedSha256": patch.proposed_sha256,
                }
            )

        manifest_contract_patches = []
        for patch in contract_patches:
            prefix = Path(patch.repository) if multiple_projects else Path()
            artifact_path = prefix / patch.relative_path
            proposed_path = Path("proposed") / artifact_path
            diff_path = Path("patches") / Path(f"{artifact_path.as_posix()}.patch")
            _write_text(temporary, proposed_path, patch.proposed_yaml)
            _write_text(temporary, diff_path, patch.unified_diff)
            manifest_contract_patches.append(
                {
                    "assetUrn": patch.asset_urn,
                    "assetName": patch.asset_name,
                    "repository": patch.repository,
                    "sourcePath": patch.relative_path.as_posix(),
                    "proposedPath": proposed_path.as_posix(),
                    "diffPath": diff_path.as_posix(),
                    "sourceSha256": patch.original_sha256,
                    "proposedSha256": patch.proposed_sha256,
                }
            )

        report = render_markdown_report(
            result,
            source_urn=source_urn,
            instruction=instruction,
            artifact_summary=artifact_summary,
        )
        _write_text(temporary, Path("impact-report.md"), report)
        source_match = DATASET_URN_RE.match(source_urn)
        source_platform = source_match.group(1) if source_match else "unknown"
        source_dataset = source_match.group(2) if source_match else source_urn
        source_owners = tuple(getattr(result, "source_owners", ()))
        review_assignments = _review_assignments(
            source_urn=source_urn,
            source_name=source_dataset,
            source_owners=source_owners,
            downstream=result.impact.downstream,
            patches=patches,
            protected=protected,
            unresolved=unresolved,
        )
        column_assets = sum(
            asset.get("evidence") == "column" for asset in result.impact.downstream
        )
        manifest = {
            "schemaVersion": 6,
            "source": {
                "urn": source_urn,
                "dataset": source_dataset,
                "platform": source_platform,
                "instruction": instruction,
                "rename": {
                    "oldColumn": rename.old_column,
                    "newColumn": rename.new_column,
                },
                "owners": list(source_owners),
            },
            "generator": result.generator,
            "migration": "migration.sql",
            "impactReport": "impact-report.md",
            "campaignReplay": "campaign.html",
            "dataHubContext": {
                "interface": "official MCP server",
                "transport": "stdio",
                "capture": getattr(result, "context_source", "provided"),
                "maxHops": getattr(result, "max_hops", None),
                "reads": [
                    {
                        "tool": "list_schema_fields",
                        "detail": (
                            f"Confirmed `{rename.old_column}` in the source schema "
                            f"({len(getattr(result, 'schema_fields', ()))} field(s) returned)."
                            if getattr(result, "schema_fields", ())
                            else "Source schema validation was not recorded in this bundle."
                        ),
                    },
                    {
                        "tool": "get_lineage(column)",
                        "detail": (
                            f"Confirmed {column_assets} downstream asset(s) using "
                            f"column-level evidence within {getattr(result, 'max_hops', None) or 'the configured'} hop limit."
                        ),
                    },
                    {
                        "tool": "get_lineage(asset)",
                        "detail": (
                            f"Retained {len(result.impact.downstream)} total reachable asset(s), "
                            "including consumers without confirmed column usage."
                        ),
                    },
                    {
                        "tool": "get_entities(ownership)",
                        "detail": (
                            "Resolved "
                            f"{sum(len(item['reviewers']) for item in review_assignments)} "
                            f"reviewer assignment(s) across {len(review_assignments)} "
                            "source and downstream asset(s)."
                            if result.impact.downstream
                            and all(
                                "owners" in asset
                                for asset in result.impact.downstream
                            )
                            else "Ownership lookup was not recorded in this bundle."
                        ),
                    },
                ],
            },
            "lineageAssets": [
                {
                    "urn": asset.get("urn"),
                    "type": asset.get("type"),
                    "name": asset.get("name"),
                    "platform": asset.get("platform"),
                    "degree": asset.get("degree"),
                    "columns": list(asset.get("columns") or ()),
                    "evidence": asset.get("evidence"),
                    "owners": list(asset.get("owners") or ()),
                }
                for asset in result.impact.downstream
            ],
            "reviewAssignments": review_assignments,
            "modelMappingResolutions": [
                resolution.to_manifest()
                for resolution in model_mapping_resolutions
            ],
            "targetRepositories": [project.name for project in projects],
            "downstreamPatches": manifest_patches,
            "contractPatches": manifest_contract_patches,
            "protectedByContract": [
                {
                    "assetUrn": asset.asset_urn,
                    "assetName": asset.asset_name,
                    "reason": asset.reason,
                    "boundaryAssets": list(asset.boundary_assets),
                }
                for asset in protected
            ],
            "manualReview": [
                {
                    "assetUrn": asset.asset_urn,
                    "assetName": asset.asset_name,
                    "reason": asset.reason,
                }
                for asset in unresolved
            ],
            "validations": [
                {
                    "id": check.check_id,
                    "status": check.status,
                    "detail": check.detail,
                }
                for check in validations
            ],
            "dbtValidation": (
                dbt_validation.to_manifest()
                if dbt_validation is not None
                else {
                    "status": "not-requested",
                    "adapter": "duckdb",
                    "dbtVersion": "unknown",
                    "isolation": "not executed",
                    "projects": [],
                }
            ),
            "readyForReview": ready_for_review,
            "safety": {
                "sqlExecuted": False,
                "targetRepositoriesModified": False,
                "humanReviewRequired": True,
                "downstreamOutputContractMayChange": downstream_mode == "propagate",
                "transitiveConsumersModified": (
                    downstream_mode == "propagate"
                    and any((asset.get("degree") or 0) > 1 for asset in result.impact.downstream)
                ),
                "dbtValidationExecuted": dbt_validation is not None,
            },
            "downstreamColumnMode": downstream_mode,
        }
        _write_text(
            temporary,
            Path("manifest.json"),
            f"{json.dumps(manifest, indent=2, sort_keys=True)}\n",
        )
        replay = render_replay_html((load_campaign(temporary),))
        _write_text(temporary, Path("campaign.html"), replay)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return ChangeBundle(
        output_dir=destination,
        patches=patches,
        contract_patches=contract_patches,
        protected=protected,
        unresolved=unresolved,
        validations=validations,
        artifact_summary=artifact_summary,
        downstream_mode=downstream_mode,
        ready_for_review=ready_for_review,
        dbt_validation=dbt_validation,
        model_mapping_resolutions=model_mapping_resolutions,
    )
