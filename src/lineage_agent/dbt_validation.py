"""Run generated dbt repairs in isolated DuckDB-backed workspaces."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ruamel.yaml import YAML


IGNORED_WORKSPACE_NAMES = {".git", "dbt_packages", "logs", "target"}
SUMMARY_RE = re.compile(
    r"Done\.\s+PASS=(\d+)\s+WARN=(\d+)\s+ERROR=(\d+)\s+SKIP=(\d+)\s+TOTAL=(\d+)",
    re.IGNORECASE,
)
VERSION_RE = re.compile(r"Running with dbt[= ]([0-9][^\s]*)", re.IGNORECASE)


@dataclass(frozen=True)
class DbtValidationStep:
    name: str
    command: tuple[str, ...]
    exit_code: int
    status: str
    summary: str
    output_tail: str


@dataclass(frozen=True)
class DbtProjectValidation:
    repository: str
    status: str
    steps: tuple[DbtValidationStep, ...]


@dataclass(frozen=True)
class DbtValidation:
    status: str
    adapter: str
    dbt_version: str
    isolation: str
    projects: tuple[DbtProjectValidation, ...]

    def to_manifest(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "adapter": self.adapter,
            "dbtVersion": self.dbt_version,
            "isolation": self.isolation,
            "projects": [
                {
                    "repository": project.repository,
                    "status": project.status,
                    "steps": [
                        {
                            "name": step.name,
                            "command": list(step.command),
                            "exitCode": step.exit_code,
                            "status": step.status,
                            "summary": step.summary,
                            "outputTail": step.output_tail,
                        }
                        for step in project.steps
                    ],
                }
                for project in self.projects
            ],
        }


def _yaml_object(path: Path) -> dict[str, Any]:
    yaml = YAML(typ="safe")
    try:
        value = yaml.load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read dbt configuration {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"dbt configuration must contain a mapping: {path}")
    return value


def _copy_project(source: Path, destination: Path) -> None:
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*IGNORED_WORKSPACE_NAMES),
    )


def _profiles(projects: Mapping[str, Path], database_path: Path) -> str:
    profile_names: list[str] = []
    for root in projects.values():
        config = _yaml_object(root / "dbt_project.yml")
        profile = config.get("profile")
        if not isinstance(profile, str) or not profile.strip():
            raise ValueError(f"dbt project does not declare a profile: {root}")
        if profile not in profile_names:
            profile_names.append(profile)
    lines: list[str] = []
    normalized_database = database_path.as_posix().replace("'", "''")
    for profile in profile_names:
        lines.extend(
            (
                f"{profile}:",
                "  target: validation",
                "  outputs:",
                "    validation:",
                "      type: duckdb",
                f"      path: '{normalized_database}'",
                "      threads: 1",
            )
        )
    return "\n".join(lines) + "\n"


def _local_dependencies(projects: Mapping[str, Path]) -> set[str]:
    by_root = {root.resolve(): name for name, root in projects.items()}
    dependencies: set[str] = set()
    for root in projects.values():
        packages_path = root / "packages.yml"
        if not packages_path.is_file():
            continue
        packages = _yaml_object(packages_path).get("packages") or []
        if not isinstance(packages, list):
            raise ValueError(f"packages must contain a list: {packages_path}")
        for package in packages:
            if not isinstance(package, dict) or not isinstance(package.get("local"), str):
                continue
            dependency_root = (root / package["local"]).resolve()
            dependency_name = by_root.get(dependency_root)
            if dependency_name:
                dependencies.add(dependency_name)
    return dependencies


def _display_command(command: Sequence[str], work_root: Path) -> tuple[str, ...]:
    displayed = ["dbt"]
    root_text = str(work_root)
    for value in command[1:]:
        displayed.append(str(value).replace(root_text, "<workspace>"))
    return tuple(displayed)


def _bounded_output(output: str, work_root: Path, limit: int = 4000) -> str:
    normalized = output.replace(str(work_root), "<workspace>").replace("\r\n", "\n")
    lines = [line.rstrip() for line in normalized.splitlines() if line.strip()]
    tail = "\n".join(lines[-40:])
    return tail[-limit:]


def _run_step(
    name: str,
    command: list[str],
    *,
    work_root: Path,
    environment: dict[str, str],
    timeout_seconds: int,
) -> DbtValidationStep:
    try:
        completed = subprocess.run(
            command,
            cwd=work_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        output = f"{completed.stdout}\n{completed.stderr}".strip()
        match = SUMMARY_RE.search(output)
        summary = (
            "PASS={0} WARN={1} ERROR={2} SKIP={3} TOTAL={4}".format(*match.groups())
            if match
            else ("Command completed successfully." if completed.returncode == 0 else "Command failed; inspect the bounded output.")
        )
        return DbtValidationStep(
            name=name,
            command=_display_command(command, work_root),
            exit_code=completed.returncode,
            status="passed" if completed.returncode == 0 else "failed",
            summary=summary,
            output_tail=_bounded_output(output, work_root),
        )
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part or ""
            for part in (exc.stdout, exc.stderr)
        )
        return DbtValidationStep(
            name=name,
            command=_display_command(command, work_root),
            exit_code=124,
            status="failed",
            summary=f"Command exceeded the {timeout_seconds}-second validation timeout.",
            output_tail=_bounded_output(output, work_root),
        )


def validate_dbt_campaign(
    projects: Mapping[str, Path],
    proposed_files: Sequence[tuple[str, Path, str]],
    *,
    dbt_executable: str | Path = "dbt",
    timeout_seconds: int = 240,
) -> DbtValidation:
    """Build proposed repositories in disposable DuckDB workspaces."""
    executable = shutil.which(str(dbt_executable))
    if executable is None:
        candidate = Path(dbt_executable).expanduser()
        if candidate.is_file():
            executable = str(candidate.resolve())
    if executable is None:
        missing = DbtValidationStep(
            name="dbt-build",
            command=("dbt", "build"),
            exit_code=127,
            status="failed",
            summary="The requested dbt executable was not found.",
            output_tail="Install requirements-validation.txt or provide --dbt-executable.",
        )
        return DbtValidation(
            status="failed",
            adapter="duckdb",
            dbt_version="unknown",
            isolation="temporary copied repositories and a disposable DuckDB database",
            projects=(DbtProjectValidation("validation", "failed", (missing,)),),
        )

    source_projects = {name: root.resolve() for name, root in projects.items()}
    with tempfile.TemporaryDirectory(prefix="blastradius-dbt-") as temp_dir:
        work_root = Path(temp_dir).resolve()
        workspace_root = work_root / "repositories"
        copied_projects: dict[str, Path] = {}
        for name, source in source_projects.items():
            destination = workspace_root / name
            _copy_project(source, destination)
            copied_projects[name] = destination

        for repository, relative_path, content in proposed_files:
            project_root = copied_projects.get(repository)
            if project_root is None:
                raise ValueError(f"Proposed dbt file references an unknown repository: {repository}")
            destination = (project_root / relative_path).resolve()
            try:
                destination.relative_to(project_root)
            except ValueError:
                raise ValueError(
                    f"Proposed dbt file escapes repository {repository}: {relative_path}"
                ) from None
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8", newline="\n")

        profiles_dir = work_root / "profiles"
        profiles_dir.mkdir()
        database_path = work_root / "validation.duckdb"
        (profiles_dir / "profiles.yml").write_text(
            _profiles(copied_projects, database_path),
            encoding="utf-8",
            newline="\n",
        )

        dependencies = _local_dependencies(copied_projects)
        validation_roots = [
            (name, root) for name, root in copied_projects.items() if name not in dependencies
        ] or list(copied_projects.items())
        environment = os.environ.copy()
        environment.update(
            {
                "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
                "PYTHONUTF8": "1",
            }
        )
        project_results: list[DbtProjectValidation] = []
        detected_version = "unknown"
        for repository, project_root in validation_roots:
            common = [
                "--project-dir",
                str(project_root),
                "--profiles-dir",
                str(profiles_dir),
                "--no-use-colors",
            ]
            steps: list[DbtValidationStep] = []
            if (project_root / "packages.yml").is_file():
                deps = _run_step(
                    "dbt-deps",
                    [executable, "deps", *common],
                    work_root=work_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
                steps.append(deps)
            if not steps or steps[-1].status == "passed":
                seed = _run_step(
                    "dbt-seed",
                    [
                        executable,
                        "seed",
                        *common,
                        "--target-path",
                        str(work_root / "target" / repository),
                        "--log-path",
                        str(work_root / "logs" / repository),
                    ],
                    work_root=work_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
                steps.append(seed)
            if steps[-1].status == "passed":
                build = _run_step(
                    "dbt-build",
                    [
                        executable,
                        "build",
                        *common,
                        "--target-path",
                        str(work_root / "target" / repository),
                        "--log-path",
                        str(work_root / "logs" / repository),
                        "--exclude",
                        "resource_type:seed",
                    ],
                    work_root=work_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                )
                steps.append(build)
                version_match = VERSION_RE.search(build.output_tail)
                if version_match:
                    detected_version = version_match.group(1)
            project_results.append(
                DbtProjectValidation(
                    repository=repository,
                    status="passed" if all(step.status == "passed" for step in steps) else "failed",
                    steps=tuple(steps),
                )
            )

        overall = "passed" if project_results and all(
            project.status == "passed" for project in project_results
        ) else "failed"
        return DbtValidation(
            status=overall,
            adapter="duckdb",
            dbt_version=detected_version,
            isolation="temporary copied repositories and a disposable DuckDB database",
            projects=tuple(project_results),
        )


def _bundle_file(bundle: Path, relative: str) -> Path:
    candidate = (bundle / relative).resolve()
    try:
        candidate.relative_to(bundle)
    except ValueError:
        raise ValueError(f"Bundle artifact escapes its directory: {relative}") from None
    if not candidate.is_file():
        raise ValueError(f"Bundle artifact does not exist: {relative}")
    return candidate


def validate_bundle(
    bundle_dir: str | Path,
    target_repos: Sequence[str | Path],
    *,
    dbt_executable: str | Path = "dbt",
) -> DbtValidation:
    """Re-run executable validation for an existing campaign bundle."""
    bundle = Path(bundle_dir).expanduser().resolve()
    manifest_path = _bundle_file(bundle, "manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bundle manifest is not valid JSON: {manifest_path}") from exc
    projects: dict[str, Path] = {}
    for value in target_repos:
        root = Path(value).expanduser().resolve()
        if not (root / "dbt_project.yml").is_file():
            raise ValueError(f"Target repository is not a dbt project: {root}")
        if root.name in projects:
            raise ValueError(f"Target repository labels must be unique: {root.name}")
        projects[root.name] = root

    proposed_files: list[tuple[str, Path, str]] = []
    for key in ("downstreamPatches", "contractPatches"):
        entries = manifest.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"Bundle manifest field {key!r} must contain a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Bundle manifest contains an invalid {key} entry")
            repository = entry.get("repository")
            source_path = entry.get("sourcePath")
            proposed_path = entry.get("proposedPath")
            if not all(isinstance(value, str) for value in (repository, source_path, proposed_path)):
                raise ValueError(f"Bundle manifest contains an incomplete {key} entry")
            proposed_files.append(
                (
                    repository,
                    Path(source_path),
                    _bundle_file(bundle, proposed_path).read_text(encoding="utf-8"),
                )
            )
    return validate_dbt_campaign(
        projects,
        proposed_files,
        dbt_executable=dbt_executable,
    )


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run an existing BlastRadius campaign in isolated dbt/DuckDB workspaces."
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--target-repo", action="append", required=True)
    parser.add_argument("--dbt-executable", default="dbt")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    try:
        result = validate_bundle(
            args.bundle,
            args.target_repo,
            dbt_executable=args.dbt_executable,
        )
    except ValueError as exc:
        print(f"BlastRadius could not validate the campaign: {exc}")
        raise SystemExit(2) from exc
    print(json.dumps(result.to_manifest(), indent=2))
    if result.status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
