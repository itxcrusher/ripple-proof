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
ENVIRONMENT_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
)
SENSITIVE_ENVIRONMENT_RE = re.compile(
    r"(?:API[_-]?KEY|AUTH|CREDENTIAL|PASSWORD|PRIVATE[_-]?KEY|SECRET|TOKEN)",
    re.IGNORECASE,
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|credential)"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
URI_PASSWORD_RE = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s]+(@)", re.IGNORECASE)
KNOWN_TOKEN_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
MODEL_RESULT_STATUS = "success"
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
    expected_nodes: tuple[str, ...] = ()
    validated_nodes: tuple[str, ...] = ()


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
                    "expectedNodes": list(project.expected_nodes),
                    "validatedNodes": list(project.validated_nodes),
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
    allowed_root = database_path.parent.resolve().as_posix()
    allowed_directories = json.dumps(
        [allowed_root],
        ensure_ascii=False,
    ).replace("'", "''")
    yaml_allowed_directories = json.dumps(allowed_directories, ensure_ascii=False)
    # dbt-duckdb reapplies settings on every cursor. Closing each handle's
    # connection keeps the allowlist-before-lockdown order repeatable.
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
                "      keep_open: false",
                "      settings:",
                "        autoinstall_known_extensions: false",
                "        autoload_known_extensions: false",
                f"        allowed_directories: {yaml_allowed_directories}",
                "        enable_external_access: false",
            )
        )
    return "\n".join(lines) + "\n"


def _local_dependencies(projects: Mapping[str, Path]) -> dict[str, set[str]]:
    by_root = {root.resolve(): name for name, root in projects.items()}
    dependencies = {name: set() for name in projects}
    for name, root in projects.items():
        for filename in ("packages.yml", "dependencies.yml"):
            packages_path = root / filename
            if not packages_path.is_file():
                continue
            dependency_config = _yaml_object(packages_path)
            if dependency_config.get("projects"):
                raise ValueError(
                    "Network-reliant dbt projects are not allowed during validation: "
                    f"{packages_path}."
                )
            packages = dependency_config.get("packages") or []
            if not isinstance(packages, list):
                raise ValueError(f"packages must contain a list: {packages_path}")
            for package in packages:
                if not isinstance(package, dict) or not isinstance(package.get("local"), str):
                    raise ValueError(
                        "Network-reliant dbt packages are not allowed during validation; "
                        f"use an explicitly supplied local project in {packages_path}."
                    )
                dependency_root = (root / package["local"]).resolve()
                dependency_name = by_root.get(dependency_root)
                if dependency_name is None:
                    raise ValueError(
                        f"Local dbt package is outside the supplied project set: {dependency_root}"
                    )
                dependencies[name].add(dependency_name)
    return dependencies


def _dependency_closure(name: str, dependencies: Mapping[str, set[str]]) -> set[str]:
    included = {name}
    pending = [name]
    while pending:
        current = pending.pop()
        for dependency in dependencies.get(current, set()):
            if dependency not in included:
                included.add(dependency)
                pending.append(dependency)
    return included


def _project_names(projects: Mapping[str, Path]) -> dict[str, str]:
    names: dict[str, str] = {}
    for label, root in projects.items():
        name = _yaml_object(root / "dbt_project.yml").get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"dbt project does not declare a name: {root}")
        names[label] = name.strip()
    return names


def _validation_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {
        name: inherited[name]
        for name in ENVIRONMENT_ALLOWLIST
        if name in inherited and inherited[name]
    }
    environment.update(
        {
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
            "DO_NOT_TRACK": "1",
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return environment


def _redaction_values(source: Mapping[str, str] | None = None) -> tuple[str, ...]:
    inherited = os.environ if source is None else source
    return tuple(
        sorted(
            {
                value
                for name, value in inherited.items()
                if len(value) >= 8 and SENSITIVE_ENVIRONMENT_RE.search(name)
            },
            key=len,
            reverse=True,
        )
    )


def _redact_output(output: str, sensitive_values: Sequence[str] = ()) -> str:
    redacted = output
    for value in sensitive_values:
        redacted = redacted.replace(value, "<redacted>")
    redacted = BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = SENSITIVE_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        redacted,
    )
    redacted = URI_PASSWORD_RE.sub(r"\1<redacted>\2", redacted)
    for pattern in KNOWN_TOKEN_RES:
        redacted = pattern.sub("<redacted>", redacted)
    return redacted


def _display_command(command: Sequence[str], work_root: Path) -> tuple[str, ...]:
    displayed = ["dbt"]
    root_text = str(work_root)
    for value in command[1:]:
        displayed.append(str(value).replace(root_text, "<workspace>"))
    return tuple(displayed)


def _bounded_output(
    output: str,
    work_root: Path,
    limit: int = 4000,
    sensitive_values: Sequence[str] = (),
) -> str:
    normalized = _redact_output(output, sensitive_values)
    normalized = normalized.replace(str(work_root), "<workspace>").replace("\r\n", "\n")
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
    sensitive_values: Sequence[str] = (),
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
            output_tail=_bounded_output(output, work_root, sensitive_values=sensitive_values),
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
            output_tail=_bounded_output(output, work_root, sensitive_values=sensitive_values),
        )


def _artifact_object(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"dbt {label} is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"dbt {label} is not valid JSON: {path}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"dbt {label} must contain an object: {path}")
    return value


def _artifact_proof(
    target_path: Path,
    expected_models: Sequence[tuple[str, str]],
    *,
    work_root: Path,
    sensitive_values: Sequence[str] = (),
) -> tuple[DbtValidationStep, tuple[str, ...], tuple[str, ...]]:
    displayed_command = (
        "dbt",
        "artifact-proof",
        "--target-path",
        str(target_path).replace(str(work_root), "<workspace>"),
    )
    failures: list[str] = []
    expected_ids: list[str] = []
    validated_ids: list[str] = []
    if not expected_models:
        failures.append("No generated dbt model was in this validation root's project closure.")
    try:
        manifest = _artifact_object(target_path / "manifest.json", "manifest")
        run_results = _artifact_object(target_path / "run_results.json", "run results")
    except ValueError as exc:
        failures.append(str(exc))
        manifest = {}
        run_results = {}

    nodes = manifest.get("nodes") or {}
    if not isinstance(nodes, Mapping):
        failures.append("dbt manifest nodes must contain an object.")
        nodes = {}
    for package_name, source_path in sorted(set(expected_models)):
        normalized_path = Path(source_path).as_posix()
        matches = [
            str(unique_id)
            for unique_id, node in nodes.items()
            if isinstance(node, Mapping)
            and node.get("resource_type") == "model"
            and node.get("package_name") == package_name
            and Path(str(node.get("original_file_path") or "")).as_posix() == normalized_path
        ]
        if len(matches) != 1:
            failures.append(
                f"Expected exactly one generated model for {package_name}:{normalized_path}; "
                f"dbt manifest contained {len(matches)}."
            )
            continue
        expected_ids.append(matches[0])

    results = run_results.get("results") or []
    if not isinstance(results, list):
        failures.append("dbt run results must contain a list.")
        results = []
    if not results:
        failures.append("dbt reported success but executed zero nodes.")
    results_by_id: dict[str, Mapping[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for result in results:
        if not isinstance(result, Mapping) or not isinstance(result.get("unique_id"), str):
            failures.append("dbt run results contain an invalid node entry.")
            continue
        unique_id = str(result["unique_id"])
        if unique_id in results_by_id:
            duplicate_ids.add(unique_id)
        results_by_id[unique_id] = result
    if duplicate_ids:
        duplicate_list = ", ".join(sorted(duplicate_ids))
        failures.append(f"dbt run results contain duplicate nodes: {duplicate_list}.")
    for unique_id in expected_ids:
        result = results_by_id.get(unique_id)
        if result is None:
            failures.append(f"Expected generated node was not executed: {unique_id}.")
        elif result.get("status") != MODEL_RESULT_STATUS:
            failures.append(
                f"Expected generated node did not succeed: {unique_id} "
                f"({result.get('status') or 'unknown'})."
            )
        else:
            validated_ids.append(unique_id)

    expected = tuple(sorted(expected_ids))
    validated = tuple(sorted(validated_ids))
    if expected != validated:
        failures.append("Validated generated nodes do not exactly match the expected node set.")
    if failures:
        detail = " ".join(dict.fromkeys(failures))
        step = DbtValidationStep(
            name="dbt-artifact-proof",
            command=displayed_command,
            exit_code=1,
            status="failed",
            summary="dbt artifacts did not prove every expected generated model.",
            output_tail=_bounded_output(
                detail,
                work_root,
                sensitive_values=sensitive_values,
            ),
        )
    else:
        step = DbtValidationStep(
            name="dbt-artifact-proof",
            command=displayed_command,
            exit_code=0,
            status="passed",
            summary=f"Proved {len(validated)} expected generated model node(s) in dbt artifacts.",
            output_tail="Validated nodes: " + ", ".join(validated),
        )
    return step, expected, validated


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
    proposed_model_paths: dict[str, set[str]] = {name: set() for name in source_projects}
    with tempfile.TemporaryDirectory(prefix="ripple-proof-dbt-") as temp_dir:
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
            if destination.suffix.lower() == ".sql":
                proposed_model_paths[repository].add(relative_path.as_posix())

        profiles_dir = work_root / "profiles"
        profiles_dir.mkdir()
        database_path = work_root / "validation.duckdb"
        (profiles_dir / "profiles.yml").write_text(
            _profiles(copied_projects, database_path),
            encoding="utf-8",
            newline="\n",
        )

        dependencies = _local_dependencies(copied_projects)
        dependency_names = {
            dependency
            for project_dependencies in dependencies.values()
            for dependency in project_dependencies
        }
        validation_roots = [
            (name, root)
            for name, root in copied_projects.items()
            if name not in dependency_names
        ] or list(copied_projects.items())
        project_names = _project_names(copied_projects)
        runtime_home = work_root / "home"
        runtime_temp = work_root / "tmp"
        runtime_home.mkdir()
        runtime_temp.mkdir()
        environment = _validation_environment()
        environment.update(
            {
                "HOME": str(runtime_home),
                "USERPROFILE": str(runtime_home),
                "TEMP": str(runtime_temp),
                "TMP": str(runtime_temp),
                "TMPDIR": str(runtime_temp),
            }
        )
        sensitive_values = _redaction_values()
        project_results: list[DbtProjectValidation] = []
        detected_version = "unknown"
        for repository, project_root in validation_roots:
            included_projects = _dependency_closure(repository, dependencies)
            expected_models = tuple(
                (project_names[project_name], source_path)
                for project_name in sorted(included_projects)
                for source_path in sorted(proposed_model_paths[project_name])
            )
            target_path = work_root / "target" / repository
            common = [
                "--project-dir",
                str(project_root),
                "--profiles-dir",
                str(profiles_dir),
                "--no-use-colors",
            ]
            steps: list[DbtValidationStep] = []
            expected_nodes: tuple[str, ...] = ()
            validated_nodes: tuple[str, ...] = ()
            has_dependencies = any(
                (project_root / name).is_file()
                for name in ("packages.yml", "dependencies.yml")
            )
            if has_dependencies:
                deps = _run_step(
                    "dbt-deps",
                    [executable, "deps", *common],
                    work_root=work_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    sensitive_values=sensitive_values,
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
                        str(target_path),
                        "--log-path",
                        str(work_root / "logs" / repository),
                    ],
                    work_root=work_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    sensitive_values=sensitive_values,
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
                        str(target_path),
                        "--log-path",
                        str(work_root / "logs" / repository),
                        "--exclude",
                        "resource_type:seed",
                    ],
                    work_root=work_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    sensitive_values=sensitive_values,
                )
                steps.append(build)
                version_match = VERSION_RE.search(build.output_tail)
                if version_match:
                    detected_version = version_match.group(1)
                if build.status == "passed":
                    proof, expected_nodes, validated_nodes = _artifact_proof(
                        target_path,
                        expected_models,
                        work_root=work_root,
                        sensitive_values=sensitive_values,
                    )
                    steps.append(proof)
            project_results.append(
                DbtProjectValidation(
                    repository=repository,
                    status="passed" if all(step.status == "passed" for step in steps) else "failed",
                    steps=tuple(steps),
                    expected_nodes=expected_nodes,
                    validated_nodes=validated_nodes,
                )
            )

        overall = "passed" if project_results and all(
            project.status == "passed" for project in project_results
        ) else "failed"
        return DbtValidation(
            status=overall,
            adapter="duckdb",
            dbt_version=detected_version,
            isolation=(
                "temporary copied repositories, a disposable DuckDB database with "
                "filesystem access restricted to the disposable validation root, "
                "local-only dependencies, and a minimal environment"
            ),
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
        description="Run an existing RippleProof campaign in isolated dbt/DuckDB workspaces."
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
        print(f"RippleProof could not validate the campaign: {exc}")
        raise SystemExit(2) from exc
    print(json.dumps(result.to_manifest(), indent=2))
    if result.status != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
