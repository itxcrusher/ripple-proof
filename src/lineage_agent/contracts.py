"""Generate round-trip-safe dbt schema contract patches."""

from __future__ import annotations

import difflib
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from .bundling import ModelPatch, UnresolvedAsset


IGNORED_DIRECTORIES = {"dbt_packages", "logs", "target", ".git"}


@dataclass(frozen=True)
class ContractPatch:
    asset_urns: tuple[str, ...]
    asset_names: tuple[str, ...]
    repository: str
    source_path: Path
    relative_path: Path
    original_yaml: str
    proposed_yaml: str
    unified_diff: str
    original_sha256: str
    proposed_sha256: str

    @property
    def asset_urn(self) -> str:
        """Retain the legacy single-asset view for artifact renderers."""
        return self.asset_urns[0]

    @property
    def asset_name(self) -> str:
        """Retain the legacy single-asset view for artifact renderers."""
        return self.asset_names[0]


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


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


def _yaml() -> YAML:
    parser = YAML(typ="rt")
    parser.preserve_quotes = True
    parser.indent(mapping=2, sequence=4, offset=2)
    return parser


def _schema_files(project_root: Path) -> tuple[Path, ...]:
    models_root = project_root / "models"
    if not models_root.is_dir():
        return ()
    return tuple(
        path
        for pattern in ("*.yml", "*.yaml")
        for path in sorted(models_root.rglob(pattern))
        if not any(part in IGNORED_DIRECTORIES for part in path.parts)
    )


def _matching_contracts(
    project_root: Path,
    model_name: str,
    old_column: str,
) -> list[tuple[Path, object, object, list[object]]]:
    matches: list[tuple[Path, object, object, list[object]]] = []
    for path in _schema_files(project_root):
        parser = _yaml()
        try:
            document = parser.load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Could not parse dbt schema file {path}: {exc}") from exc
        if not isinstance(document, dict):
            continue
        models = document.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if (
                not isinstance(model, dict)
                or str(model.get("name") or "").casefold()
                != model_name.casefold()
            ):
                continue
            columns = model.get("columns")
            if not isinstance(columns, list):
                continue
            for column in columns:
                if (
                    isinstance(column, dict)
                    and str(column.get("name") or "").casefold()
                    == old_column.casefold()
                ):
                    matches.append((path, document, column, columns))
    return matches


def plan_contract_patches(
    projects: dict[str, Path],
    model_patches: tuple[ModelPatch, ...],
    old_column: str,
    new_column: str,
    downstream_mode: str,
) -> tuple[tuple[ContractPatch, ...], tuple[UnresolvedAsset, ...]]:
    """Patch declared dbt columns when the output rename is propagated."""
    if downstream_mode == "preserve":
        return (), ()

    planned_by_file: dict[tuple[str, Path], list[ModelPatch]] = {}
    unresolved: list[UnresolvedAsset] = []
    for model_patch in model_patches:
        project_root = projects[model_patch.repository]
        matches = _matching_contracts(
            project_root,
            model_patch.source_path.stem,
            old_column,
        )
        if not matches:
            continue
        if len(matches) > 1:
            choices = ", ".join(
                path.relative_to(project_root).as_posix()
                for path, _document, _column, _columns in matches
            )
            unresolved.append(
                UnresolvedAsset(
                    model_patch.asset_urn,
                    model_patch.asset_name,
                    f"Multiple dbt contract declarations match `{old_column}`: {choices}.",
                )
            )
            continue

        path, _document, column, columns = matches[0]
        destination_exists = any(
            candidate is not column
            and isinstance(candidate, dict)
            and str(candidate.get("name") or "").casefold()
            == new_column.casefold()
            for candidate in columns
        )
        if destination_exists:
            unresolved.append(
                UnresolvedAsset(
                    model_patch.asset_urn,
                    model_patch.asset_name,
                    f"The dbt contract already declares `{new_column}`; refusing "
                    f"to create a duplicate column in {path.relative_to(project_root)}.",
                )
            )
            continue

        planned_by_file.setdefault(
            (model_patch.repository, path.resolve()), []
        ).append(model_patch)

    patches: list[ContractPatch] = []
    for (repository, path), file_model_patches in planned_by_file.items():
        project_root = projects[repository]
        original = path.read_text(encoding="utf-8")
        parser = _yaml()
        try:
            document = parser.load(original)
        except Exception as exc:
            raise ValueError(f"Could not parse dbt schema file {path}: {exc}") from exc

        for model_patch in file_model_patches:
            matches = []
            if isinstance(document, dict):
                models = document.get("models")
                if isinstance(models, list):
                    for model in models:
                        if (
                            not isinstance(model, dict)
                            or str(model.get("name") or "").casefold()
                            != model_patch.source_path.stem.casefold()
                        ):
                            continue
                        columns = model.get("columns")
                        if not isinstance(columns, list):
                            continue
                        for column in columns:
                            if (
                                isinstance(column, dict)
                                and str(column.get("name") or "").casefold()
                                == old_column.casefold()
                            ):
                                matches.append((column, columns))

            if len(matches) != 1:
                raise ValueError(
                    "The dbt contract changed while the campaign was being planned: "
                    f"expected one `{old_column}` declaration for "
                    f"{model_patch.source_path.stem} in {path}."
                )
            column, columns = matches[0]
            if any(
                candidate is not column
                and isinstance(candidate, dict)
                and str(candidate.get("name") or "").casefold()
                == new_column.casefold()
                for candidate in columns
            ):
                raise ValueError(
                    "The dbt contract changed while the campaign was being planned: "
                    f"`{new_column}` now exists for {model_patch.source_path.stem} "
                    f"in {path}."
                )
            column["name"] = new_column

        output = io.StringIO()
        parser.dump(document, output)
        proposed = output.getvalue()
        relative_path = path.relative_to(project_root)
        patches.append(
            ContractPatch(
                asset_urns=tuple(
                    model_patch.asset_urn for model_patch in file_model_patches
                ),
                asset_names=tuple(
                    model_patch.asset_name for model_patch in file_model_patches
                ),
                repository=repository,
                source_path=path,
                relative_path=relative_path,
                original_yaml=original,
                proposed_yaml=proposed,
                unified_diff=_diff(relative_path, original, proposed),
                original_sha256=_sha256(original),
                proposed_sha256=_sha256(proposed),
            )
        )
    return tuple(patches), tuple(unresolved)
