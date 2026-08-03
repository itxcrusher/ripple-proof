"""Exercise a real PostgreSQL destination-column collision in CI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from lineage_agent.postgres_validation import validate_postgres_migration


def _bundle_inputs(bundle: Path) -> tuple[str, str, str, str, tuple[str, ...]]:
    root = bundle.expanduser().resolve()
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read campaign manifest: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise ValueError("Campaign manifest must contain an object")
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Campaign manifest does not contain a source object")
    rename = source.get("rename")
    if not isinstance(rename, Mapping):
        raise ValueError("Campaign manifest does not contain a rename intent")
    source_urn = source.get("urn")
    old_column = rename.get("oldColumn")
    new_column = rename.get("newColumn")
    if not all(isinstance(value, str) for value in (source_urn, old_column, new_column)):
        raise ValueError("Campaign source rename is incomplete")
    migration_name = manifest.get("migration")
    if not isinstance(migration_name, str):
        raise ValueError("Campaign manifest does not identify its migration")
    migration_path = (root / migration_name).resolve()
    try:
        migration_path.relative_to(root)
    except ValueError:
        raise ValueError("Campaign migration escapes its bundle") from None
    migration_sql = migration_path.read_text(encoding="utf-8")
    context = manifest.get("dataHubContext")
    schema_fields: tuple[str, ...] = ()
    if isinstance(context, Mapping):
        raw_fields = context.get("schemaFields") or ()
        if isinstance(raw_fields, list) and all(isinstance(value, str) for value in raw_fields):
            schema_fields = tuple(raw_fields)
    return source_urn, old_column, new_column, migration_sql, schema_fields


def _is_expected_collision(result: Any, fixture_columns: tuple[str, ...]) -> bool:
    migration = next(
        (step for step in result.steps if step.name == "migration-transaction"),
        None,
    )
    cleanup = next(
        (step for step in result.steps if step.name == "container-cleanup"),
        None,
    )
    return (
        result.status == "failed"
        and result.before_columns == fixture_columns
        and migration is not None
        and migration.status == "failed"
        and "already exists" in migration.output_tail.lower()
        and cleanup is not None
        and cleanup.status == "passed"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Prove PostgreSQL refuses a rename when the destination column exists."
    )
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--docker-executable", default="docker")
    parser.add_argument("--image", default="postgres:16")
    args = parser.parse_args(argv)
    source_urn, old_column, new_column, migration_sql, schema_fields = _bundle_inputs(
        args.bundle
    )
    fixture_columns = tuple(dict.fromkeys((*schema_fields, old_column, new_column)))
    result = validate_postgres_migration(
        source_urn=source_urn,
        old_column=old_column,
        new_column=new_column,
        migration_sql=migration_sql,
        source_columns=fixture_columns,
        docker_executable=args.docker_executable,
        image=args.image,
    )
    print(json.dumps(result.to_manifest(), indent=2))
    if not _is_expected_collision(result, fixture_columns):
        raise SystemExit("PostgreSQL did not prove the expected destination-column refusal.")
    print("Expected PostgreSQL destination-column collision was refused.")


if __name__ == "__main__":
    main()
