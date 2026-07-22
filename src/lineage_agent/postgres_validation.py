"""Validate generated PostgreSQL rename SQL inside a disposable container."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


EVIDENCE_SCHEMA_VERSION = 1
DATASET_URN_RE = re.compile(
    r"^urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([^)]+)\)$"
)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
IMAGE_RE = re.compile(
    r"^(?:(?:docker\.io/)?library/)?postgres"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]*|@sha256:[a-f0-9]{64})$"
)


@dataclass(frozen=True)
class PostgresValidationStep:
    name: str
    command: tuple[str, ...]
    exit_code: int
    status: str
    summary: str
    output_tail: str


@dataclass(frozen=True)
class PostgresValidation:
    status: str
    engine: str
    engine_version: str
    image: str
    isolation: str
    source_dataset: str
    old_column: str
    new_column: str
    migration_sha256: str
    before_columns: tuple[str, ...]
    after_columns: tuple[str, ...]
    rollback_columns: tuple[str, ...]
    steps: tuple[PostgresValidationStep, ...]
    validated_at: str

    def to_manifest(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "engine": self.engine,
            "engineVersion": self.engine_version,
            "image": self.image,
            "isolation": self.isolation,
            "sourceDataset": self.source_dataset,
            "oldColumn": self.old_column,
            "newColumn": self.new_column,
            "migrationSha256": self.migration_sha256,
            "beforeColumns": list(self.before_columns),
            "afterColumns": list(self.after_columns),
            "rollbackColumns": list(self.rollback_columns),
            "steps": [
                {
                    "name": step.name,
                    "command": list(step.command),
                    "exitCode": step.exit_code,
                    "status": step.status,
                    "summary": step.summary,
                    "outputTail": step.output_tail,
                }
                for step in self.steps
            ],
            "validatedAt": self.validated_at,
        }


def _quote_identifier(identifier: str) -> str:
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"Unsupported PostgreSQL identifier: {identifier!r}")
    return f'"{identifier}"'


def _source_parts(source_urn: str) -> tuple[str, str, str]:
    match = DATASET_URN_RE.fullmatch(source_urn)
    if not match:
        raise ValueError(f"Unsupported DataHub dataset URN: {source_urn!r}")
    platform, dataset, _environment = match.groups()
    if platform.lower() != "postgres":
        raise ValueError("Disposable migration validation supports PostgreSQL sources only")
    parts = dataset.split(".")
    if len(parts) != 2 or not all(IDENTIFIER_RE.fullmatch(part) for part in parts):
        raise ValueError("PostgreSQL source dataset must be an unquoted schema.table name")
    return dataset, parts[0], parts[1]


def _expected_migration(schema: str, table: str, old_column: str, new_column: str) -> str:
    return (
        f"ALTER TABLE {_quote_identifier(schema)}.{_quote_identifier(table)} "
        f"RENAME COLUMN {_quote_identifier(old_column)} TO {_quote_identifier(new_column)};"
    )


def _bounded_output(output: str, container_name: str, limit: int = 4000) -> str:
    normalized = output.replace(container_name, "<container>").replace("\r\n", "\n")
    lines = [line.rstrip() for line in normalized.splitlines() if line.strip()]
    return "\n".join(lines[-40:])[-limit:]


def _display_command(command: Sequence[str], container_name: str) -> tuple[str, ...]:
    displayed = ["docker" if index == 0 else str(value) for index, value in enumerate(command)]
    return tuple(value.replace(container_name, "<container>") for value in displayed)


def _run_step(
    name: str,
    command: list[str],
    *,
    container_name: str,
    timeout_seconds: int,
    input_text: str | None = None,
    success_summary: str,
) -> PostgresValidationStep:
    try:
        completed = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}".strip()
        output_tail = (
            "<container-id>"
            if name == "container-start" and completed.returncode == 0
            else _bounded_output(output, container_name)
        )
        return PostgresValidationStep(
            name=name,
            command=_display_command(command, container_name),
            exit_code=completed.returncode,
            status="passed" if completed.returncode == 0 else "failed",
            summary=(
                success_summary
                if completed.returncode == 0
                else "Command failed; inspect the bounded output."
            ),
            output_tail=output_tail,
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part or ""
            for part in (exc.stdout, exc.stderr)
        )
        return PostgresValidationStep(
            name=name,
            command=_display_command(command, container_name),
            exit_code=124,
            status="failed",
            summary=f"Command exceeded the {timeout_seconds}-second validation timeout.",
            output_tail=_bounded_output(output, container_name),
        )
    except OSError as exc:
        return PostgresValidationStep(
            name=name,
            command=_display_command(command, container_name),
            exit_code=127,
            status="failed",
            summary="The Docker command could not be started.",
            output_tail=str(exc),
        )


def _columns(marker: str, output: str) -> tuple[str, ...]:
    prefix = f"{marker}:"
    for line in output.replace("\r\n", "\n").splitlines():
        value = line.strip()
        if value.startswith(prefix):
            payload = value[len(prefix) :]
            return tuple(item for item in payload.split(",") if item)
    return ()


def _marker(marker: str, output: str) -> str:
    prefix = f"{marker}:"
    for line in output.replace("\r\n", "\n").splitlines():
        value = line.strip()
        if value.startswith(prefix):
            return value[len(prefix) :]
    return "unknown"


def validate_postgres_migration(
    *,
    source_urn: str,
    old_column: str,
    new_column: str,
    migration_sql: str,
    docker_executable: str | Path = "docker",
    image: str = "postgres:16",
    timeout_seconds: int = 45,
) -> PostgresValidation:
    """Run one constrained rename inside an isolated, disposable PostgreSQL container."""
    dataset, schema, table = _source_parts(source_urn)
    expected = _expected_migration(schema, table, old_column, new_column)
    if migration_sql.strip() != expected:
        raise ValueError("Migration SQL is not the exact constrained rename for this campaign")
    if not IMAGE_RE.fullmatch(image):
        raise ValueError(
            "PostgreSQL validation accepts only an explicit official postgres image tag or digest"
        )

    executable = shutil.which(str(docker_executable))
    if executable is None:
        candidate = Path(docker_executable).expanduser()
        if candidate.is_file():
            executable = str(candidate.resolve())
    container_name = f"blastradius-postgres-{uuid.uuid4().hex[:12]}"
    migration_sha256 = hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()
    validated_at = datetime.now(timezone.utc).isoformat()
    isolation = "disposable Docker container with no network and tmpfs PostgreSQL data"
    if executable is None:
        missing = PostgresValidationStep(
            name="container-start",
            command=("docker", "run", "--rm", image),
            exit_code=127,
            status="failed",
            summary="The requested Docker executable was not found.",
            output_tail="Install Docker or provide --docker-executable.",
        )
        return PostgresValidation(
            "failed", "postgresql", "unknown", image, isolation, dataset,
            old_column, new_column, migration_sha256, (), (), (), (missing,), validated_at,
        )

    steps: list[PostgresValidationStep] = []
    before: tuple[str, ...] = ()
    after: tuple[str, ...] = ()
    rollback: tuple[str, ...] = ()
    engine_version = "unknown"
    started = False
    start = _run_step(
        "container-start",
        [
            executable,
            "run",
            "--rm",
            "--detach",
            "--pull",
            "never",
            "--network",
            "none",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,nosuid,size=256m",
            "--name",
            container_name,
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            image,
        ],
        container_name=container_name,
        timeout_seconds=timeout_seconds,
        success_summary="Started the disposable PostgreSQL container.",
    )
    steps.append(start)
    started = start.status == "passed"
    try:
        if started:
            readiness = None
            for _attempt in range(30):
                readiness = _run_step(
                    "postgres-ready",
                    [executable, "exec", container_name, "pg_isready", "-U", "postgres", "-d", "postgres"],
                    container_name=container_name,
                    timeout_seconds=5,
                    success_summary="PostgreSQL accepted local connections.",
                )
                if readiness.status == "passed":
                    break
                time.sleep(0.5)
            assert readiness is not None
            steps.append(readiness)

            psql = [
                executable,
                "exec",
                "--interactive",
                container_name,
                "psql",
                "--username",
                "postgres",
                "--dbname",
                "postgres",
                "--no-psqlrc",
                "--quiet",
                "--tuples-only",
                "--no-align",
                "--set",
                "ON_ERROR_STOP=1",
            ]
            column_query = (
                "SELECT '{marker}:' || COALESCE(string_agg(column_name, ',' ORDER BY ordinal_position), '') "
                "FROM information_schema.columns "
                f"WHERE table_schema = '{schema}' AND table_name = '{table}';"
            )
            if readiness.status == "passed":
                setup_sql = "\n".join(
                    (
                        f"CREATE SCHEMA {_quote_identifier(schema)};",
                        f"CREATE TABLE {_quote_identifier(schema)}.{_quote_identifier(table)} ({_quote_identifier(old_column)} text);",
                        "SELECT 'version:' || current_setting('server_version');",
                        column_query.format(marker="before"),
                    )
                )
                setup = _run_step(
                    "fixture-setup",
                    psql,
                    container_name=container_name,
                    timeout_seconds=timeout_seconds,
                    input_text=setup_sql,
                    success_summary="Created and inspected the pre-migration fixture.",
                )
                steps.append(setup)
                before = _columns("before", setup.output_tail)
                engine_version = _marker("version", setup.output_tail)
                if setup.status == "passed":
                    validation_sql = "\n".join(
                        (
                            "BEGIN;",
                            expected,
                            column_query.format(marker="after"),
                            "ROLLBACK;",
                            column_query.format(marker="rollback"),
                        )
                    )
                    migration = _run_step(
                        "migration-transaction",
                        psql,
                        container_name=container_name,
                        timeout_seconds=timeout_seconds,
                        input_text=validation_sql,
                        success_summary="Applied the rename, inspected it, rolled it back, and inspected the restored schema.",
                    )
                    after = _columns("after", migration.output_tail)
                    rollback = _columns("rollback", migration.output_tail)
                    expected_observations = (
                        before == (old_column,)
                        and after == (new_column,)
                        and rollback == (old_column,)
                    )
                    if migration.status == "passed" and not expected_observations:
                        migration = PostgresValidationStep(
                            migration.name,
                            migration.command,
                            1,
                            "failed",
                            "Schema observations did not prove the expected rename and rollback.",
                            migration.output_tail,
                        )
                    steps.append(migration)
    finally:
        if started:
            cleanup = _run_step(
                "container-cleanup",
                [executable, "rm", "--force", container_name],
                container_name=container_name,
                timeout_seconds=15,
                success_summary="Removed the disposable PostgreSQL container.",
            )
            steps.append(cleanup)

    status = "passed" if steps and all(step.status == "passed" for step in steps) else "failed"
    return PostgresValidation(
        status=status,
        engine="postgresql",
        engine_version=engine_version,
        image=image,
        isolation=isolation,
        source_dataset=dataset,
        old_column=old_column,
        new_column=new_column,
        migration_sha256=migration_sha256,
        before_columns=before,
        after_columns=after,
        rollback_columns=rollback,
        steps=tuple(steps),
        validated_at=validated_at,
    )


def _bundle_inputs(bundle_dir: str | Path) -> tuple[Path, dict[str, Any], bytes, str]:
    root = Path(bundle_dir).expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read campaign manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Campaign manifest must contain a JSON object")
    migration_path = manifest.get("migration")
    if not isinstance(migration_path, str):
        raise ValueError("Campaign manifest does not identify its migration")
    migration_file = (root / migration_path).resolve()
    try:
        migration_file.relative_to(root)
    except ValueError:
        raise ValueError("Campaign migration escapes its bundle") from None
    if not migration_file.is_file():
        raise ValueError("Campaign migration does not exist")
    return root, manifest, manifest_bytes, migration_file.read_text(encoding="utf-8")


def validate_bundle(
    bundle_dir: str | Path,
    *,
    docker_executable: str | Path = "docker",
    image: str = "postgres:16",
) -> PostgresValidation:
    """Validate the constrained source migration from an existing campaign."""
    _root, manifest, _manifest_bytes, migration_sql = _bundle_inputs(bundle_dir)
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
    return validate_postgres_migration(
        source_urn=source_urn,
        old_column=old_column,
        new_column=new_column,
        migration_sql=migration_sql,
        docker_executable=docker_executable,
        image=image,
    )


def validate_postgres_evidence(
    value: Mapping[str, Any],
    *,
    campaign_manifest_sha256: str,
    migration_sql: str,
    source_dataset: str,
    old_column: str,
    new_column: str,
) -> dict[str, Any]:
    """Validate a manifest-bound disposable PostgreSQL evidence sidecar."""
    if value.get("schemaVersion") != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("PostgreSQL evidence uses an unsupported schema version")
    if value.get("campaignManifestSha256") != campaign_manifest_sha256:
        raise ValueError("PostgreSQL evidence does not match the campaign manifest")
    validation = value.get("validation")
    if not isinstance(validation, Mapping):
        raise ValueError("PostgreSQL evidence does not contain a validation object")
    if not isinstance(value.get("recordedAt"), str) or not value["recordedAt"]:
        raise ValueError("PostgreSQL evidence does not contain a recording timestamp")
    expected_migration_sha = hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()
    if validation.get("migrationSha256") != expected_migration_sha:
        raise ValueError("PostgreSQL evidence does not match the campaign migration")
    if (
        validation.get("sourceDataset") != source_dataset
        or validation.get("oldColumn") != old_column
        or validation.get("newColumn") != new_column
    ):
        raise ValueError("PostgreSQL evidence does not match the campaign source rename")
    if (
        validation.get("engine") != "postgresql"
        or not isinstance(validation.get("engineVersion"), str)
        or not validation.get("engineVersion")
        or not isinstance(validation.get("image"), str)
        or not IMAGE_RE.fullmatch(validation["image"])
        or not isinstance(validation.get("isolation"), str)
        or not validation.get("isolation")
        or not isinstance(validation.get("validatedAt"), str)
        or not validation.get("validatedAt")
    ):
        raise ValueError("PostgreSQL evidence contains invalid engine metadata")
    status = validation.get("status")
    steps = validation.get("steps")
    if status not in ("passed", "failed") or not isinstance(steps, list) or not steps:
        raise ValueError("PostgreSQL evidence has an invalid status or step list")
    step_statuses: list[str] = []
    for step in steps:
        if not isinstance(step, Mapping):
            raise ValueError("PostgreSQL evidence contains an invalid step")
        step_status = step.get("status")
        exit_code = step.get("exitCode")
        command = step.get("command")
        if (
            step_status not in ("passed", "failed")
            or not isinstance(exit_code, int)
            or (step_status == "passed") != (exit_code == 0)
            or not isinstance(step.get("name"), str)
            or not step.get("name")
            or not isinstance(command, list)
            or not command
            or not all(isinstance(item, str) and item for item in command)
            or not isinstance(step.get("summary"), str)
            or not isinstance(step.get("outputTail"), str)
        ):
            raise ValueError("PostgreSQL evidence contains an invalid step")
        step_statuses.append(step_status)
    if (status == "passed") != all(item == "passed" for item in step_statuses):
        raise ValueError("PostgreSQL evidence status is inconsistent with its steps")
    if status == "passed":
        old_column = validation.get("oldColumn")
        new_column = validation.get("newColumn")
        if (
            validation.get("beforeColumns") != [old_column]
            or validation.get("afterColumns") != [new_column]
            or validation.get("rollbackColumns") != [old_column]
        ):
            raise ValueError("PostgreSQL evidence does not prove rename and rollback")
    return dict(value)


def write_postgres_evidence(
    bundle_dir: str | Path,
    validation: PostgresValidation,
    *,
    overwrite: bool = False,
) -> Path:
    """Atomically persist manifest-bound disposable PostgreSQL validation evidence."""
    root, manifest, manifest_bytes, migration_sql = _bundle_inputs(bundle_dir)
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Campaign manifest does not contain a source object")
    rename = source.get("rename")
    if not isinstance(rename, Mapping):
        raise ValueError("Campaign manifest does not contain a rename intent")
    if (
        validation.source_dataset != source.get("dataset")
        or validation.old_column != rename.get("oldColumn")
        or validation.new_column != rename.get("newColumn")
    ):
        raise ValueError("PostgreSQL validation does not belong to this campaign")
    path = root / "postgres-validation.json"
    if path.exists() and not overwrite:
        raise ValueError("PostgreSQL evidence already exists; use --force to replace it")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    evidence = {
        "schemaVersion": EVIDENCE_SCHEMA_VERSION,
        "campaignManifestSha256": manifest_sha,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "validation": validation.to_manifest(),
    }
    validate_postgres_evidence(
        evidence,
        campaign_manifest_sha256=manifest_sha,
        migration_sql=migration_sql,
        source_dataset=str(source.get("dataset") or ""),
        old_column=str(rename.get("oldColumn") or ""),
        new_column=str(rename.get("newColumn") or ""),
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
        raise ValueError(f"Could not write PostgreSQL evidence: {exc}") from exc
    return path


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a BlastRadius source migration in disposable PostgreSQL."
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--docker-executable", default="docker")
    parser.add_argument("--image", default="postgres:16")
    parser.add_argument("--record-evidence", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.force and not args.record_evidence:
        parser.error("--force requires --record-evidence")
    return args


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _arguments(argv)
    try:
        result = validate_bundle(
            args.bundle,
            docker_executable=args.docker_executable,
            image=args.image,
        )
        evidence_path = (
            write_postgres_evidence(args.bundle, result, overwrite=args.force)
            if args.record_evidence
            else None
        )
    except ValueError as exc:
        print(f"BlastRadius could not validate the PostgreSQL migration: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(result.to_manifest(), indent=2))
    if evidence_path:
        print(f"Evidence: {evidence_path}")
    if result.status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
