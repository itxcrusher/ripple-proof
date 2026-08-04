"""Build reviewable migration bundles without modifying downstream projects."""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    preserved_output_aliases: int
    remaining_executable_old_identifiers: int
    original_sql: str
    proposed_sql: str
    unified_diff: str
    original_sha256: str
    proposed_sha256: str
    repository: str = ""
    discovery: str = "filename"


@dataclass(frozen=True)
class UnresolvedAsset:
    asset_urn: str
    asset_name: str
    reason: str


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


def _sql_structure_tokens(sql: str) -> tuple[tuple[str, str], ...]:
    """Return executable identifiers and structural punctuation only.

    This intentionally is not a SQL parser. It supplies a conservative preflight
    that can reject relation-scoped shapes before the lexical rewriter is used.
    """
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(sql):
        if sql.startswith("--", index):
            end = sql.find("\n", index)
            index = len(sql) if end == -1 else end
            continue
        if sql.startswith("/*", index):
            index = _consume_until(sql, index + 2, "*/", "block comment")
            continue

        jinja_closing = None
        if sql.startswith("{{", index):
            jinja_closing = "}}"
        elif sql.startswith("{%", index):
            jinja_closing = "%}"
        elif sql.startswith("{#", index):
            jinja_closing = "#}"
        if jinja_closing:
            index = _consume_until(sql, index + 2, jinja_closing, "Jinja block")
            continue

        if sql[index] == "'":
            index = _consume_quoted(sql, index, "'")
            continue
        if sql[index] == '"':
            end = _consume_quoted(sql, index, '"')
            identifier = sql[index + 1 : end - 1].replace('""', '"')
            tokens.append(("identifier", identifier.casefold()))
            index = end
            continue

        dollar_match = DOLLAR_QUOTE_RE.match(sql, index)
        if dollar_match:
            delimiter = dollar_match.group(0)
            index = _consume_until(
                sql,
                dollar_match.end(),
                delimiter,
                "dollar-quoted body",
            )
            continue

        identifier_match = IDENTIFIER_RE.match(sql, index)
        if identifier_match:
            tokens.append(("identifier", identifier_match.group(0).casefold()))
            index = identifier_match.end()
            continue
        if sql[index] in ".,()":
            tokens.append(("punctuation", sql[index]))
        index += 1
    return tuple(tokens)


def _unsupported_automatic_repair(sql: str, old: str) -> str | None:
    """Fail closed when lexical replacement cannot prove column ownership."""
    tokens = _sql_structure_tokens(sql)
    old_folded = old.casefold()
    identifiers = [value for kind, value in tokens if kind == "identifier"]
    if old_folded not in identifiers:
        return None

    # A dbt model body is a query. UPDATE/INSERT/DELETE/MERGE introduce their
    # own relation scoping (UPDATE ... FROM, DELETE ... USING, MERGE ... USING)
    # that lexical rewriting cannot prove, so refuse anything that does not
    # begin as a SELECT or a CTE.
    if identifiers and identifiers[0] not in {"select", "with"}:
        return (
            "Automatic repair cannot prove column ownership in SQL that is not "
            "a single SELECT statement."
        )

    relation_scoped_keywords = {
        "with",
        "join",
        "lateral",
        "union",
        "intersect",
        "except",
    }
    if relation_scoped_keywords.intersection(identifiers):
        return (
            "Automatic repair cannot prove column ownership in SQL containing "
            "joins, CTEs, set operations, or lateral relations."
        )
    if identifiers.count("select") > 1:
        return (
            "Automatic repair cannot prove column ownership across nested or "
            "multi-query SQL."
        )
    # Count only relation-introducing FROM keywords. `extract(year from x)`,
    # `substring(x from 1)`, and `trim(both ' ' from x)` all put FROM inside a
    # function call, where it introduces no relation.
    relation_from_count = 0
    depth = 0
    for token in tokens:
        if token == ("punctuation", "("):
            depth += 1
        elif token == ("punctuation", ")"):
            depth = max(0, depth - 1)
        elif token == ("identifier", "from") and depth == 0:
            relation_from_count += 1
    if relation_from_count > 1:
        return (
            "Automatic repair cannot prove column ownership across nested or "
            "multi-query SQL."
        )

    qualifiers: set[str] = set()
    for index in range(2, len(tokens)):
        if tokens[index] != ("identifier", old_folded):
            continue
        if (
            tokens[index - 1] == ("punctuation", ".")
            and tokens[index - 2][0] == "identifier"
        ):
            qualifiers.add(tokens[index - 2][1])
    if len(qualifiers) > 1:
        return (
            "Automatic repair found the renamed column under multiple relation "
            "qualifiers and cannot determine which relation owns it."
        )

    from_indexes = [
        index
        for index, token in enumerate(tokens)
        if token == ("identifier", "from")
    ]
    if from_indexes:
        # Scan to the end of the statement rather than stopping at the first
        # clause keyword. Those words are matched as bare identifiers, so a
        # relation aliased `qualify` (not a PostgreSQL keyword) or a quoted
        # column named "order" ended the scan before the comma that introduces
        # a second relation, and an unrelated relation's column was rewritten.
        # A depth-0 comma after FROM is only ever a relation separator here,
        # because select-list commas precede the first FROM.
        depth = 0
        for token in tokens[from_indexes[0] + 1 :]:
            if token == ("punctuation", "("):
                depth += 1
            elif token == ("punctuation", ")"):
                depth = max(0, depth - 1)
            elif token == ("punctuation", ",") and depth == 0:
                return (
                    "Automatic repair cannot prove column ownership across "
                    "multiple FROM relations."
                )
    return None


def _preserve_simple_output_alias(
    original: str,
    proposed: str,
    old: str,
    new: str,
) -> tuple[str, int]:
    """Preserve one unambiguous, line-oriented SELECT output alias.

    Complex projection expressions are deliberately rejected by returning zero;
    callers then retain the asset for manual review instead of guessing.
    """
    original_lines = original.splitlines(keepends=True)
    proposed_lines = proposed.splitlines(keepends=True)
    old_pattern = re.escape(old)
    projection_re = re.compile(
        rf"^(?P<indent>[ \t]*)"
        rf"(?P<qualifier>(?:[A-Za-z_][A-Za-z0-9_$]*|\"[^\"]+\")[ \t]*\.[ \t]*)?"
        rf"(?P<column>{old_pattern}|\"{old_pattern}\")"
        rf"(?P<comma>[ \t]*,?)"
        rf"(?P<trailing>[ \t]*(?:--.*)?)$",
        re.IGNORECASE,
    )
    in_projection = False
    matches: list[tuple[int, re.Match[str], str]] = []

    for index, line in enumerate(original_lines):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        stripped = body.strip()
        if not in_projection:
            if re.fullmatch(r"select", stripped, re.IGNORECASE):
                in_projection = True
            continue
        if re.match(r"^from\b", stripped, re.IGNORECASE):
            break
        match = projection_re.fullmatch(body)
        if match:
            matches.append((index, match, newline))

    if len(matches) != 1:
        return proposed, 0

    index, match, newline = matches[0]
    qualifier, _qualifier_replacements = replace_sql_identifier(
        match.group("qualifier") or "",
        old,
        new,
    )
    quoted = match.group("column").startswith('"')
    new_reference = f'"{new}"' if quoted else new
    preserved_alias = f'"{old}"' if quoted else old
    proposed_lines[index] = (
        f"{match.group('indent')}{qualifier}{new_reference} as {preserved_alias}"
        f"{match.group('comma')}{match.group('trailing')}{newline}"
    )
    return "".join(proposed_lines), 1


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
    downstream_mode: str = "preserve",
    model_path_overrides: dict[str, Path] | None = None,
) -> tuple[tuple[ModelPatch, ...], tuple[UnresolvedAsset, ...]]:
    """Plan patches only for confirmed dbt dataset dependencies."""
    if downstream_mode not in {"preserve", "propagate"}:
        raise ValueError(f"Unsupported downstream column mode: {downstream_mode}")
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
        override = (model_path_overrides or {}).get(asset_urn)
        if override is not None:
            resolved_override = override.expanduser().resolve()
            try:
                resolved_override.relative_to(root)
            except ValueError:
                raise ValueError(
                    f"Model path override escapes target repository: {resolved_override}"
                ) from None
            candidates = [resolved_override] if resolved_override.is_file() else []
        else:
            candidates = [
                path
                for path in model_files
                if path.stem.casefold() == model_name.casefold()
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
        unsupported_reason = _unsupported_automatic_repair(original, old_column)
        if unsupported_reason:
            unresolved.append(
                UnresolvedAsset(asset_urn, asset_name, unsupported_reason)
            )
            continue
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

        derived_columns = {
            str(column).casefold() for column in (asset.get("columns") or ())
        }
        preserved_aliases = 0
        if downstream_mode == "preserve" and old_column.casefold() in derived_columns:
            proposed, preserved_aliases = _preserve_simple_output_alias(
                original,
                proposed,
                old_column,
                new_column,
            )
            if preserved_aliases == 0:
                unresolved.append(
                    UnresolvedAsset(
                        asset_urn,
                        asset_name,
                        "The downstream output uses the renamed column, but its SELECT "
                        "projection is not a single unambiguous identifier that can be "
                        "aliased safely.",
                    )
                )
                continue

        _unchanged, remaining_old_identifiers = replace_sql_identifier(
            proposed,
            old_column,
            new_column,
        )
        if remaining_old_identifiers != preserved_aliases:
            raise RuntimeError(
                f"Patch validation failed for {source_path}: "
                f"{remaining_old_identifiers} executable old identifier(s) remain, "
                f"but {preserved_aliases} preserved output alias(es) were expected"
            )

        relative_path = source_path.relative_to(root)
        patches.append(
            ModelPatch(
                asset_urn=asset_urn,
                asset_name=asset_name,
                source_path=source_path,
                relative_path=relative_path,
                replacement_count=replacement_count,
                preserved_output_aliases=preserved_aliases,
                remaining_executable_old_identifiers=remaining_old_identifiers,
                original_sql=original,
                proposed_sql=proposed,
                unified_diff=_diff(relative_path, original, proposed),
                original_sha256=_sha256(original),
                proposed_sha256=_sha256(proposed),
            )
        )

    return tuple(patches), tuple(unresolved)

