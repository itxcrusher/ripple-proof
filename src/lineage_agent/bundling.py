"""Build reviewable migration bundles without modifying downstream projects."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .reporting import render_markdown_report


DATASET_URN_RE = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^)]+)\)$"
)
IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
IGNORED_MODEL_DIRECTORIES = {"dbt_packages", "logs", "target", ".git"}


@dataclass(frozen=True)
class ModelPatch:
    asset_urn: str
    asset_name: str
    source_path: Path
    relative_path: Path
    replacement_count: int
    original_sql: str
    proposed_sql: str
    unified_diff: str
    original_sha256: str
    proposed_sha256: str


@dataclass(frozen=True)
class UnresolvedAsset:
    asset_urn: str
    asset_name: str
    reason: str


@dataclass(frozen=True)
class ChangeBundle:
    output_dir: Path
    patches: tuple[ModelPatch, ...]
    unresolved: tuple[UnresolvedAsset, ...]
    artifact_summary: str


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _consume_quoted(sql: str, start: int, quote: str) -> int:
    index = start + 1
    while index < len(sql):
        if sql[index] != quote:
            index += 1
            continue
        if index + 1 < len(sql) and sql[index + 1] == quote:
            index += 2
            continue
        return index + 1
    raise ValueError(f"Unterminated {quote} quote in downstream SQL")


def _consume_until(sql: str, start: int, closing: str, label: str) -> int:
    end = sql.find(closing, start)
    if end == -1:
        raise ValueError(f"Unterminated {label} in downstream SQL")
    return end + len(closing)


def replace_sql_identifier(sql: str, old: str, new: str) -> tuple[str, int]:
    """Replace executable SQL identifiers while preserving inert text.

    String literals, SQL comments, dollar-quoted bodies, and dbt/Jinja blocks are
    copied byte-for-byte. PostgreSQL double-quoted identifiers are eligible.
    """
    output: list[str] = []
    index = 0
    replacements = 0
    old_folded = old.casefold()

    while index < len(sql):
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            end = len(sql) if end == -1 else end
            output.append(sql[index:end])
            index = end
            continue

        if sql.startswith("/*", index):
            end = _consume_until(sql, index + 2, "*/", "block comment")
            output.append(sql[index:end])
            index = end
            continue

        jinja_closing = None
        if sql.startswith("{{", index):
            jinja_closing = "}}"
        elif sql.startswith("{%", index):
            jinja_closing = "%}"
        elif sql.startswith("{#", index):
            jinja_closing = "#}"
        if jinja_closing:
            end = _consume_until(sql, index + 2, jinja_closing, "Jinja block")
            output.append(sql[index:end])
            index = end
            continue

        if sql[index] == "'":
            end = _consume_quoted(sql, index, "'")
            output.append(sql[index:end])
            index = end
            continue

        if sql[index] == '"':
            end = _consume_quoted(sql, index, '"')
            quoted = sql[index + 1 : end - 1].replace('""', '"')
            if quoted.casefold() == old_folded:
                escaped = new.replace('"', '""')
                output.append(f'"{escaped}"')
                replacements += 1
            else:
                output.append(sql[index:end])
            index = end
            continue

        dollar_match = DOLLAR_QUOTE_RE.match(sql, index)
        if dollar_match:
            delimiter = dollar_match.group(0)
            end = _consume_until(
                sql,
                dollar_match.end(),
                delimiter,
                "dollar-quoted body",
            )
            output.append(sql[index:end])
            index = end
            continue

        identifier_match = IDENTIFIER_RE.match(sql, index)
        if identifier_match:
            identifier = identifier_match.group(0)
            if identifier.casefold() == old_folded:
                output.append(new)
                replacements += 1
            else:
                output.append(identifier)
            index = identifier_match.end()
            continue

        output.append(sql[index])
        index += 1

    return "".join(output), replacements


def _dataset_name(asset: dict[str, Any]) -> str:
    name = str(asset.get("name") or "").strip()
    if not name:
        match = DATASET_URN_RE.match(str(asset.get("urn") or ""))
        if match:
            name = match.group(2)
    leaf = name.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    return leaf


def _model_files(project_root: Path) -> tuple[Path, ...]:
    models_root = project_root / "models"
    if not models_root.is_dir():
        raise ValueError(f"No dbt models directory found at {models_root}")
    return tuple(
        path
        for path in sorted(models_root.rglob("*.sql"))
        if not any(part in IGNORED_MODEL_DIRECTORIES for part in path.parts)
    )


def _diff(relative_path: Path, original: str, proposed: str) -> str:
    relative = relative_path.as_posix()
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
        )
    )


def plan_model_patches(
    project_root: str | Path,
    downstream: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    old_column: str,
    new_column: str,
) -> tuple[tuple[ModelPatch, ...], tuple[UnresolvedAsset, ...]]:
    """Plan patches only for confirmed dbt dataset dependencies."""
    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Target repository does not exist: {root}")
    model_files = _model_files(root)
    patches: list[ModelPatch] = []
    unresolved: list[UnresolvedAsset] = []

    for asset in downstream:
        asset_urn = str(asset.get("urn") or "unknown")
        asset_name = str(asset.get("name") or asset_urn)
        if asset.get("evidence") != "column":
            unresolved.append(
                UnresolvedAsset(
                    asset_urn,
                    asset_name,
                    "DataHub reports asset-level reachability but not column usage.",
                )
            )
            continue
        if str(asset.get("type") or "").upper() != "DATASET" or str(
            asset.get("platform") or ""
        ).casefold() != "dbt":
            unresolved.append(
                UnresolvedAsset(
                    asset_urn,
                    asset_name,
                    "Automatic patches currently support confirmed dbt datasets only.",
                )
            )
            continue

        model_name = _dataset_name(asset)
        candidates = [
            path for path in model_files if path.stem.casefold() == model_name.casefold()
        ]
        if not candidates:
            unresolved.append(
                UnresolvedAsset(
                    asset_urn,
                    asset_name,
                    f"No dbt model file named {model_name}.sql was found.",
                )
            )
            continue
        if len(candidates) > 1:
            choices = ", ".join(path.relative_to(root).as_posix() for path in candidates)
            unresolved.append(
                UnresolvedAsset(
                    asset_urn,
                    asset_name,
                    f"Multiple dbt model files match {model_name}: {choices}.",
                )
            )
            continue

        source_path = candidates[0]
        original = source_path.read_text(encoding="utf-8")
        proposed, replacement_count = replace_sql_identifier(
            original,
            old_column,
            new_column,
        )
        if replacement_count == 0:
            unresolved.append(
                UnresolvedAsset(
                    asset_urn,
                    asset_name,
                    f"The model contains no executable `{old_column}` identifier.",
                )
            )
            continue

        _unchanged, remaining_references = replace_sql_identifier(
            proposed,
            old_column,
            new_column,
        )
        if remaining_references:
            raise RuntimeError(
                f"Patch validation failed for {source_path}: "
                f"{remaining_references} executable source reference(s) remain"
            )

        relative_path = source_path.relative_to(root)
        patches.append(
            ModelPatch(
                asset_urn=asset_urn,
                asset_name=asset_name,
                source_path=source_path,
                relative_path=relative_path,
                replacement_count=replacement_count,
                original_sql=original,
                proposed_sql=proposed,
                unified_diff=_diff(relative_path, original, proposed),
                original_sha256=_sha256(original),
                proposed_sha256=_sha256(proposed),
            )
        )

    return tuple(patches), tuple(unresolved)


def _artifact_summary(
    patches: tuple[ModelPatch, ...],
    unresolved: tuple[UnresolvedAsset, ...],
) -> str:
    lines = []
    if patches:
        lines.append("Proposed downstream patches:")
        for patch in patches:
            lines.append(
                f"- `{patch.relative_path.as_posix()}` for `{patch.asset_name}` "
                f"({patch.replacement_count} executable reference(s) updated)"
            )
    else:
        lines.append("No downstream code patch could be generated automatically.")
    if unresolved:
        lines.append("\nAssets requiring manual review:")
        for asset in unresolved:
            lines.append(f"- `{asset.asset_name}`: {asset.reason}")
    lines.append(
        "\nPatch mode: propagate the rename through executable references. "
        "This may change the downstream model's output contract."
    )
    lines.append(
        "The source repository was not modified. Proposed files and diffs require human review."
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
    target_repo: str | Path,
    output_dir: str | Path,
) -> ChangeBundle:
    """Atomically create migration, patch, report, and manifest artifacts."""
    rename = getattr(result, "rename", None)
    if rename is None:
        raise ValueError("The generated change does not contain a resolved rename intent")

    target_root = Path(target_repo).expanduser().resolve()
    destination = Path(output_dir).expanduser().resolve()
    try:
        destination.relative_to(target_root)
    except ValueError:
        pass
    else:
        raise ValueError(
            "Bundle output must be outside the target repository so source files "
            "remain untouched"
        )
    if destination.exists():
        raise FileExistsError(f"Bundle output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    patches, unresolved = plan_model_patches(
        target_root,
        result.impact.downstream,
        rename.old_column,
        rename.new_column,
    )
    artifact_summary = _artifact_summary(patches, unresolved)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    try:
        _write_text(temporary, Path("migration.sql"), f"{result.change_sql}\n")
        for patch in patches:
            proposed_path = Path("proposed") / patch.relative_path
            diff_path = Path("patches") / Path(f"{patch.relative_path.as_posix()}.patch")
            _write_text(temporary, proposed_path, patch.proposed_sql)
            _write_text(temporary, diff_path, patch.unified_diff)

        report = render_markdown_report(
            result,
            source_urn=source_urn,
            instruction=instruction,
            artifact_summary=artifact_summary,
        )
        _write_text(temporary, Path("impact-report.md"), report)

        manifest = {
            "schemaVersion": 1,
            "source": {"urn": source_urn, "instruction": instruction},
            "generator": result.generator,
            "migration": "migration.sql",
            "impactReport": "impact-report.md",
            "downstreamPatches": [
                {
                    "assetUrn": patch.asset_urn,
                    "assetName": patch.asset_name,
                    "sourcePath": patch.relative_path.as_posix(),
                    "proposedPath": (
                        Path("proposed") / patch.relative_path
                    ).as_posix(),
                    "diffPath": (
                        Path("patches")
                        / Path(f"{patch.relative_path.as_posix()}.patch")
                    ).as_posix(),
                    "replacementCount": patch.replacement_count,
                    "remainingExecutableSourceReferences": 0,
                    "sourceSha256": patch.original_sha256,
                    "proposedSha256": patch.proposed_sha256,
                }
                for patch in patches
            ],
            "manualReview": [
                {
                    "assetUrn": asset.asset_urn,
                    "assetName": asset.asset_name,
                    "reason": asset.reason,
                }
                for asset in unresolved
            ],
            "safety": {
                "sqlExecuted": False,
                "targetRepositoryModified": False,
                "humanReviewRequired": True,
                "downstreamOutputContractMayChange": True,
            },
            "downstreamColumnMode": "propagate",
        }
        _write_text(
            temporary,
            Path("manifest.json"),
            f"{json.dumps(manifest, indent=2, sort_keys=True)}\n",
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise

    return ChangeBundle(
        output_dir=destination,
        patches=patches,
        unresolved=unresolved,
        artifact_summary=artifact_summary,
    )
