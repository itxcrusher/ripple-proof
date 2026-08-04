"""Publish an approved campaign as review-only GitHub pull requests."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .context_guard import (
    CapturedContext,
    ContextVerification,
    captured_context_from_manifest,
    require_fresh_context,
    verify_fresh_context,
)
from .replay import validate_publication_evidence


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class PublicationError(RuntimeError):
    """A campaign could not be safely prepared or published."""


class ExistingBranchError(PublicationError):
    """An exact recovery check is required for an existing campaign branch."""


@dataclass(frozen=True)
class PlannedFile:
    source_path: Path
    proposed_path: Path
    source_sha256: str
    proposed_sha256: str


@dataclass(frozen=True)
class RepositoryPlan:
    label: str
    repository: str
    base_branch: str
    head_branch: str
    files: tuple[PlannedFile, ...]


@dataclass(frozen=True)
class PublicationPlan:
    bundle: Path
    manifest_sha256: str
    mode: str
    title: str
    body: str
    commit_message: str
    repositories: tuple[RepositoryPlan, ...]
    datahub_context: CapturedContext

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaignManifestSha256": self.manifest_sha256,
            "mode": self.mode,
            "title": self.title,
            "mergePolicy": "manual-only",
            "dataHubContextSha256": self.datahub_context.fingerprint_sha256,
            "repositories": [
                {
                    "label": repository.label,
                    "repository": repository.repository,
                    "baseBranch": repository.base_branch,
                    "headBranch": repository.head_branch,
                    "files": [file.source_path.as_posix() for file in repository.files],
                }
                for repository in self.repositories
            ],
        }


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _bundle_file(bundle: Path, relative: str) -> Path:
    candidate = (bundle / relative).resolve()
    try:
        candidate.relative_to(bundle)
    except ValueError:
        raise ValueError(f"Bundle artifact escapes its directory: {relative}") from None
    if not candidate.is_file():
        raise ValueError(f"Bundle artifact does not exist: {relative}")
    return candidate


def _manifest(bundle: Path) -> tuple[dict[str, Any], str]:
    manifest_path = _bundle_file(bundle, "manifest.json")
    manifest_bytes = manifest_path.read_bytes()
    try:
        value = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bundle manifest is not valid JSON: {manifest_path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Bundle manifest must contain an object")
    return value, _sha256_bytes(manifest_bytes)


def parse_repository_maps(values: Sequence[str]) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for value in values:
        label, separator, repository = value.partition("=")
        if not separator or not label or not REPOSITORY_RE.fullmatch(repository):
            raise ValueError(
                "Repository mappings must use campaign_label=owner/repository"
            )
        if label in mappings:
            raise ValueError(f"Duplicate repository mapping: {label}")
        mappings[label] = repository
    return mappings


def _one_line(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Campaign manifest must provide {label}")
    return " ".join(value.split())


def prepare_publication(
    bundle_dir: str | Path,
    repository_maps: Mapping[str, str],
    *,
    base_branch: str = "main",
) -> PublicationPlan:
    """Create a read-only publication plan from a validated campaign."""
    bundle = Path(bundle_dir).expanduser().resolve()
    manifest, manifest_sha256 = _manifest(bundle)
    datahub_context = captured_context_from_manifest(manifest)
    if not manifest.get("readyForReview"):
        raise ValueError("Campaign is not ready for review")
    failed = [
        check.get("id", "unknown")
        for check in manifest.get("validations") or []
        if isinstance(check, dict) and check.get("status") == "failed"
    ]
    if failed:
        raise ValueError(f"Campaign has failed validation gates: {', '.join(failed)}")
    dbt_validation = manifest.get("dbtValidation") or {}
    if not isinstance(dbt_validation, dict) or dbt_validation.get("status") != "passed":
        raise ValueError("Campaign must pass executable dbt validation before publication")
    if not BRANCH_RE.fullmatch(base_branch):
        raise ValueError(f"Invalid base branch: {base_branch}")

    source = manifest.get("source") or {}
    rename = source.get("rename") or {}
    dataset = _one_line(source.get("dataset"), "source dataset")
    old_column = _one_line(rename.get("oldColumn"), "old column")
    new_column = _one_line(rename.get("newColumn"), "new column")
    if not IDENTIFIER_RE.fullmatch(old_column) or not IDENTIFIER_RE.fullmatch(new_column):
        raise ValueError("Campaign rename columns are not valid identifiers")
    mode = _one_line(manifest.get("downstreamColumnMode"), "downstream mode")
    head_branch = f"ripple-proof/{mode}-{manifest_sha256[:12]}"

    grouped: dict[str, list[PlannedFile]] = {}
    for key in ("downstreamPatches", "contractPatches"):
        entries = manifest.get(key) or []
        if not isinstance(entries, list):
            raise ValueError(f"Bundle manifest field {key!r} must contain a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"Bundle manifest contains an invalid {key} entry")
            required = (
                "repository",
                "sourcePath",
                "proposedPath",
                "sourceSha256",
                "proposedSha256",
            )
            if not all(isinstance(entry.get(field), str) for field in required):
                raise ValueError(f"Bundle manifest contains an incomplete {key} entry")
            if not LABEL_RE.fullmatch(entry["repository"]):
                raise ValueError(f"Unsafe campaign repository label: {entry['repository']}")
            proposed = _bundle_file(bundle, entry["proposedPath"])
            if _sha256_bytes(proposed.read_bytes()) != entry["proposedSha256"]:
                raise ValueError(f"Proposed artifact hash does not match: {entry['proposedPath']}")
            grouped.setdefault(entry["repository"], []).append(
                PlannedFile(
                    source_path=Path(entry["sourcePath"]),
                    proposed_path=Path(entry["proposedPath"]),
                    source_sha256=entry["sourceSha256"],
                    proposed_sha256=entry["proposedSha256"],
                )
            )
    if not grouped:
        raise ValueError("Campaign does not contain publishable file changes")
    missing = sorted(set(grouped) - set(repository_maps))
    if missing:
        raise ValueError(f"Missing GitHub repository mappings: {', '.join(missing)}")

    repositories: list[RepositoryPlan] = []
    for label in sorted(grouped):
        remote = repository_maps[label]
        if not REPOSITORY_RE.fullmatch(remote):
            raise ValueError(f"Invalid GitHub repository mapping for {label}: {remote}")
        repositories.append(
            RepositoryPlan(
                label=label,
                repository=remote,
                base_branch=base_branch,
                head_branch=head_branch,
                files=tuple(sorted(grouped[label], key=lambda file: file.source_path.as_posix())),
            )
        )

    title = f"fix: apply {dataset} {old_column} to {new_column} rename"
    body_lines = [
        "## Change campaign",
        "",
        f"Applies the reviewed downstream repairs for `{dataset}.{old_column}` to `{new_column}`.",
        "",
        f"- Campaign manifest SHA-256: `{manifest_sha256}`",
        f"- Downstream policy: `{mode}`",
        "- Migration execution: not performed",
        "- Merge policy: human review only",
        "",
        "The pull request contains only files assigned to this repository. Weak-evidence assets remain outside automatic repair.",
    ]
    return PublicationPlan(
        bundle=bundle,
        manifest_sha256=manifest_sha256,
        mode=mode,
        title=title,
        body="\n".join(body_lines) + "\n",
        commit_message=f"fix: update downstream {new_column} references",
        repositories=tuple(repositories),
        datahub_context=datahub_context,
    )


def _run(
    command: Sequence[str | Path],
    *,
    cwd: Path | None = None,
    timeout_seconds: int = 180,
    allowed_exit_codes: tuple[int, ...] = (0,),
) -> CommandResult:
    values = tuple(str(value) for value in command)
    try:
        completed = subprocess.run(
            values,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PublicationError(
            f"Command timed out after {timeout_seconds}s: {values[0]}"
        ) from exc
    result = CommandResult(values, completed.returncode, completed.stdout, completed.stderr)
    if result.exit_code not in allowed_exit_codes:
        detail = (result.stderr or result.stdout).strip()[-2000:]
        raise PublicationError(
            f"Command failed with exit code {result.exit_code}: {values[0]}\n{detail}"
        )
    return result


def _json_result(result: CommandResult, label: str) -> Any:
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PublicationError(f"{label} did not return valid JSON") from exc


def _verify_branch_checkout(
    repository: RepositoryPlan,
    checkout: Path,
) -> str:
    expected = sorted(file.source_path.as_posix() for file in repository.files)
    changed = _run(
        (
            "git",
            "diff",
            "--name-only",
            f"origin/{repository.base_branch}...HEAD",
        ),
        cwd=checkout,
    ).stdout.splitlines()
    if sorted(changed) != expected:
        raise PublicationError(
            f"Published branch differs from the approved campaign for {repository.repository}"
        )
    for file in repository.files:
        proposed = (checkout / file.source_path).resolve()
        try:
            proposed.relative_to(checkout)
        except ValueError:
            raise PublicationError(
                f"Published file escapes repository {repository.label}: {file.source_path}"
            ) from None
        if (
            not proposed.is_file()
            or _sha256_bytes(proposed.read_bytes()) != file.proposed_sha256
        ):
            raise PublicationError(
                f"Published file does not match the approved proposal: "
                f"{repository.repository}/{file.source_path}"
            )
    commit_sha = _run(("git", "rev-parse", "HEAD"), cwd=checkout).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}", commit_sha):
        raise PublicationError(
            f"Published branch returned an invalid commit for {repository.repository}"
        )
    return commit_sha


def _validate_campaign_pull_request(
    repository: RepositoryPlan,
    pull_request: Mapping[str, Any],
) -> dict[str, Any]:
    number = pull_request.get("number")
    expected_url = (
        f"https://github.com/{repository.repository}/pull/{number}"
        if isinstance(number, int)
        else None
    )
    if (
        not isinstance(number, int)
        or number < 1
        or pull_request.get("state") != "OPEN"
        or pull_request.get("headRefName") != repository.head_branch
        or pull_request.get("baseRefName") != repository.base_branch
        or pull_request.get("autoMergeRequest") is not None
        or pull_request.get("url") != expected_url
    ):
        raise PublicationError(
            f"Campaign pull request is closed, wrong, or not manual-only for "
            f"{repository.repository}"
        )
    return dict(pull_request)


def _find_campaign_pull_request(
    repository: RepositoryPlan,
    *,
    gh_executable: str,
    allow_missing: bool,
) -> dict[str, Any] | None:
    listed = _json_result(
        _run(
            (
                gh_executable,
                "pr",
                "list",
                "--repo",
                repository.repository,
                "--head",
                repository.head_branch,
                "--state",
                "all",
                "--json",
                "number,url,state,headRefName,baseRefName,autoMergeRequest",
            )
        ),
        "GitHub pull request list",
    )
    if not isinstance(listed, list) or not all(
        isinstance(item, dict) for item in listed
    ):
        raise PublicationError("GitHub pull request list must return a list")
    if len(listed) > 1:
        raise PublicationError(
            f"Multiple campaign pull requests exist for {repository.repository}:"
            f"{repository.head_branch}"
        )
    if not listed:
        if allow_missing:
            return None
        raise PublicationError(
            f"Expected exactly one campaign pull request in {repository.repository}"
        )
    return _validate_campaign_pull_request(repository, listed[0])


def _recover_unjournaled_repository(
    plan: PublicationPlan,
    repository: RepositoryPlan,
    root: Path,
    *,
    gh_executable: str,
) -> tuple[Path, str, dict[str, Any] | None]:
    """Adopt exact pushed state without ever rewriting the remote branch."""
    recovery_root = Path(
        tempfile.mkdtemp(prefix="recovery-", dir=root)
    ).resolve()
    checkout = recovery_root / repository.label
    _run(
        (
            gh_executable,
            "repo",
            "clone",
            repository.repository,
            checkout,
            "--",
            "--branch",
            repository.head_branch,
        )
    )
    _run(("git", "fetch", "origin", repository.base_branch), cwd=checkout)
    commit_sha = _verify_branch_checkout(repository, checkout)
    pull_request = _find_campaign_pull_request(
        repository,
        gh_executable=gh_executable,
        allow_missing=True,
    )
    return checkout, commit_sha, pull_request


def _preflight_repository(
    plan: PublicationPlan,
    repository: RepositoryPlan,
    root: Path,
    *,
    gh_executable: str,
) -> tuple[Path, str]:
    checkout = root / repository.label
    _run(
        (
            gh_executable,
            "repo",
            "clone",
            repository.repository,
            checkout,
            "--",
            "--depth",
            "1",
            "--branch",
            repository.base_branch,
        )
    )
    existing_branch = _run(
        ("git", "ls-remote", "--exit-code", "--heads", "origin", repository.head_branch),
        cwd=checkout,
        allowed_exit_codes=(0, 2),
    )
    if existing_branch.exit_code == 0:
        raise ExistingBranchError(
            f"Remote branch already exists and will not be overwritten: "
            f"{repository.repository}:{repository.head_branch}"
        )
    _run(("git", "checkout", "-b", repository.head_branch), cwd=checkout)
    for file in repository.files:
        destination = (checkout / file.source_path).resolve()
        try:
            destination.relative_to(checkout)
        except ValueError:
            raise PublicationError(
                f"Campaign file escapes repository {repository.label}: {file.source_path}"
            ) from None
        if not destination.is_file():
            raise PublicationError(
                f"Remote source file does not exist: {repository.repository}/{file.source_path}"
            )
        if _sha256_bytes(destination.read_bytes()) != file.source_sha256:
            raise PublicationError(
                f"Remote source drifted from the approved campaign: "
                f"{repository.repository}/{file.source_path}"
            )
        proposed = _bundle_file(plan.bundle, file.proposed_path).read_bytes()
        if _sha256_bytes(proposed) != file.proposed_sha256:
            raise PublicationError(f"Approved proposal hash changed: {file.proposed_path}")
        destination.write_bytes(proposed)
        _run(("git", "add", "--", file.source_path.as_posix()), cwd=checkout)
    _run(("git", "diff", "--cached", "--check"), cwd=checkout)
    changed = _run(("git", "diff", "--cached", "--name-only"), cwd=checkout).stdout.splitlines()
    expected = sorted(file.source_path.as_posix() for file in repository.files)
    if sorted(changed) != expected:
        raise PublicationError(
            f"Prepared files differ from the approved campaign for {repository.repository}"
        )
    _run(("git", "commit", "-m", plan.commit_message), cwd=checkout)
    commit_sha = _run(("git", "rev-parse", "HEAD"), cwd=checkout).stdout.strip()
    return checkout, commit_sha


def _checks(
    repository: str,
    pull_request_url: str,
    *,
    gh_executable: str,
    timeout_seconds: int,
    required_checks: Sequence[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    checks: list[Any] = []
    last_exit_code = 8
    while True:
        listed = _run(
            (
                gh_executable,
                "pr",
                "checks",
                pull_request_url,
                "--repo",
                repository,
                "--json",
                "name,state,bucket,link,workflow,startedAt,completedAt",
            ),
            allowed_exit_codes=(0, 1, 8),
        )
        last_exit_code = listed.exit_code
        # `gh pr checks` answers with plain text, not JSON, while a pull request
        # has no registered checks yet ("no checks reported", or a GraphQL
        # resolution message for a just-opened pull request), and it can do so
        # with exit code 0. That is a not-ready-yet signal, so keep polling
        # until the deadline instead of failing an otherwise valid campaign.
        try:
            value = json.loads(listed.stdout)
        except json.JSONDecodeError:
            value = None
        if value is None:
            if time.monotonic() >= deadline:
                status = "pending"
                break
            time.sleep(min(5, max(0, deadline - time.monotonic())))
            continue
        if not isinstance(value, list):
            raise PublicationError("GitHub check query must return a list")
        checks = value
        buckets = {check.get("bucket") for check in checks if isinstance(check, dict)}
        names = {check.get("name") for check in checks if isinstance(check, dict)}
        if buckets & {"fail", "cancel"}:
            status = "failed"
            break
        if set(required_checks) <= names and buckets <= {"pass", "skipping"}:
            status = "passed"
            break
        if time.monotonic() >= deadline:
            status = "pending"
            break
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    return {
        "status": status,
        "waitExitCode": last_exit_code,
        "timedOut": status == "pending",
        "requiredChecks": list(required_checks),
        "checks": checks,
    }


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _progress_path(evidence_path: Path) -> Path:
    return evidence_path.with_name(
        f"{evidence_path.stem}.progress{evidence_path.suffix}"
    )


def _repository_record(
    repository: RepositoryPlan,
    commit_sha: str,
    pull_request: Mapping[str, Any],
    checks: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "label": repository.label,
        "repository": repository.repository,
        "baseBranch": repository.base_branch,
        "headBranch": repository.head_branch,
        "commitSha": commit_sha,
        "files": [
            {
                "path": file.source_path.as_posix(),
                "sha256": file.proposed_sha256,
            }
            for file in repository.files
        ],
        "autoMergeEnabled": False,
        "pullRequest": dict(pull_request),
        "checks": dict(checks),
    }


def _publication_progress(
    plan: PublicationPlan,
    repositories: Sequence[Mapping[str, Any]],
    *,
    required_checks: Sequence[str],
    context_verification: ContextVerification,
    started_at: str,
) -> dict[str, Any]:
    completed_labels = [str(item["label"]) for item in repositories]
    all_labels = [repository.label for repository in plan.repositories]
    return {
        "schemaVersion": 1,
        "campaignManifestSha256": plan.manifest_sha256,
        "status": (
            "completed" if completed_labels == all_labels else "in-progress"
        ),
        "mergePolicy": "manual-only",
        "autoMergeEnabled": False,
        "requiredChecks": list(required_checks),
        "startedAt": started_at,
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "dataHubContextVerification": context_verification.to_dict(),
        "completedRepositories": completed_labels,
        "remainingRepositories": all_labels[len(completed_labels):],
        "repositories": [dict(item) for item in repositories],
    }


def _load_publication_progress(
    progress_path: Path,
    plan: PublicationPlan,
    *,
    required_checks: Sequence[str],
) -> tuple[list[dict[str, Any]], str]:
    if not progress_path.is_file():
        return [], datetime.now(timezone.utc).isoformat()
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(
            f"Publication progress is unreadable: {progress_path}"
        ) from exc
    if not isinstance(progress, dict):
        raise PublicationError("Publication progress must contain a JSON object")
    records = progress.get("repositories")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise PublicationError("Publication progress contains invalid repositories")
    completed = progress.get("completedRepositories")
    labels = [str(item.get("label")) for item in records]
    expected_prefix = [repository.label for repository in plan.repositories[: len(records)]]
    all_labels = [repository.label for repository in plan.repositories]
    if (
        progress.get("schemaVersion") != 1
        or progress.get("campaignManifestSha256") != plan.manifest_sha256
        or progress.get("mergePolicy") != "manual-only"
        or progress.get("autoMergeEnabled") is not False
        or progress.get("requiredChecks") != list(required_checks)
        or completed != labels
        or labels != expected_prefix
        or progress.get("remainingRepositories") != all_labels[len(records):]
        or progress.get("status")
        != ("completed" if labels == all_labels else "in-progress")
    ):
        raise PublicationError(
            "Publication progress is not bound to the exact current campaign plan"
        )
    started_at = progress.get("startedAt")
    if not isinstance(started_at, str) or not started_at:
        raise PublicationError("Publication progress does not contain a start timestamp")
    return [dict(item) for item in records], started_at


def _assert_resumed_repository(
    plan: PublicationPlan,
    repository: RepositoryPlan,
    recorded: Mapping[str, Any],
    *,
    commit_sha: str,
    pull_request: Mapping[str, Any],
) -> None:
    expected_files = [
        {"path": file.source_path.as_posix(), "sha256": file.proposed_sha256}
        for file in repository.files
    ]
    recorded_pr = recorded.get("pullRequest")
    if (
        recorded.get("label") != repository.label
        or recorded.get("repository") != repository.repository
        or recorded.get("baseBranch") != repository.base_branch
        or recorded.get("headBranch") != repository.head_branch
        or recorded.get("commitSha") != commit_sha
        or recorded.get("files") != expected_files
        or recorded.get("autoMergeEnabled") is not False
        or not isinstance(recorded_pr, dict)
        or recorded_pr.get("number") != pull_request.get("number")
        or recorded_pr.get("url") != pull_request.get("url")
        or recorded_pr.get("headRefName") != pull_request.get("headRefName")
        or recorded_pr.get("baseRefName") != pull_request.get("baseRefName")
    ):
        raise PublicationError(
            f"Existing publication no longer matches recorded progress for "
            f"{repository.repository}"
        )


def publish_publication(
    plan: PublicationPlan,
    *,
    approved_manifest_sha256: str,
    evidence_path: str | Path | None = None,
    gh_executable: str = "gh",
    check_timeout_seconds: int = 300,
    required_checks: Sequence[str] = (),
    context_verification: ContextVerification | None = None,
) -> dict[str, Any]:
    """Open review-only pull requests, journaling exact resumable progress."""
    if approved_manifest_sha256 != plan.manifest_sha256:
        raise ValueError(
            "Approval hash does not match the current campaign manifest; rerun the dry-run"
        )
    if not required_checks:
        raise ValueError("At least one required GitHub check must be named")
    require_fresh_context(plan.datahub_context, context_verification)
    executable = shutil.which(gh_executable)
    if executable is None:
        raise ValueError(f"GitHub CLI executable was not found: {gh_executable}")
    evidence_destination = (
        Path(evidence_path).expanduser().resolve()
        if evidence_path is not None
        else plan.bundle / "publication.json"
    )
    progress_destination = _progress_path(evidence_destination)
    recorded, started_at = _load_publication_progress(
        progress_destination,
        plan,
        required_checks=required_checks,
    )

    with tempfile.TemporaryDirectory(prefix="ripple-proof-pr-") as temp_dir:
        root = Path(temp_dir).resolve()
        published: list[dict[str, Any]] = []

        # A resume never trusts its journal alone. Re-clone every completed branch,
        # compare the complete diff and file hashes, and re-query the exact open PR.
        for index, recorded_repository in enumerate(recorded):
            repository = plan.repositories[index]
            commit_sha, pull_request = _verify_published_repository(
                plan,
                repository,
                root,
                gh_executable=executable,
            )
            _assert_resumed_repository(
                plan,
                repository,
                recorded_repository,
                commit_sha=commit_sha,
                pull_request=pull_request,
            )
            published.append(
                _repository_record(
                    repository,
                    commit_sha,
                    pull_request,
                    _checks(
                        repository.repository,
                        pull_request["url"],
                        gh_executable=executable,
                        timeout_seconds=check_timeout_seconds,
                        required_checks=required_checks,
                    ),
                )
            )

        remaining = plan.repositories[len(recorded):]
        prepared: list[
            tuple[RepositoryPlan, Path, str, dict[str, Any] | None, bool]
        ] = []
        # Preserve all-preflight-before-first-new-mutation. Existing unjournaled
        # branches are never overwritten: they are cloned, exhaustively compared
        # with the approved proposal, and their all-state PR set is inspected.
        for repository in remaining:
            try:
                checkout, commit_sha = _preflight_repository(
                    plan,
                    repository,
                    root,
                    gh_executable=executable,
                )
            except ExistingBranchError:
                checkout, commit_sha, recovered_pull_request = (
                    _recover_unjournaled_repository(
                        plan,
                        repository,
                        root,
                        gh_executable=executable,
                    )
                )
                prepared.append(
                    (
                        repository,
                        checkout,
                        commit_sha,
                        recovered_pull_request,
                        False,
                    )
                )
            else:
                prepared.append((repository, checkout, commit_sha, None, True))

        body_path = root / "pull-request.md"
        body_path.write_text(plan.body, encoding="utf-8", newline="\n")
        for (
            repository,
            checkout,
            commit_sha,
            recovered_pull_request,
            requires_push,
        ) in prepared:
            if requires_push:
                _run(
                    (
                        "git",
                        "push",
                        "--set-upstream",
                        "origin",
                        repository.head_branch,
                    ),
                    cwd=checkout,
                )
            if recovered_pull_request is None:
                created = _run(
                    (
                        executable,
                        "pr",
                        "create",
                        "--repo",
                        repository.repository,
                        "--base",
                        repository.base_branch,
                        "--head",
                        repository.head_branch,
                        "--title",
                        plan.title,
                        "--body-file",
                        body_path,
                    )
                )
                output_lines = created.stdout.strip().splitlines()
                if not output_lines:
                    raise PublicationError(
                        "GitHub CLI did not return a pull request URL"
                    )
                pull_request_url = output_lines[-1]
                if not pull_request_url.startswith("https://github.com/"):
                    raise PublicationError(
                        "GitHub CLI returned an unexpected pull request URL"
                    )
                viewed_value = _json_result(
                    _run(
                        (
                            executable,
                            "pr",
                            "view",
                            pull_request_url,
                            "--repo",
                            repository.repository,
                            "--json",
                            "number,url,state,headRefName,baseRefName,"
                            "autoMergeRequest",
                        )
                    ),
                    "GitHub pull request query",
                )
                if not isinstance(viewed_value, dict):
                    raise PublicationError(
                        "GitHub pull request query must return an object"
                    )
                viewed = _validate_campaign_pull_request(
                    repository, viewed_value
                )
                if viewed["url"] != pull_request_url:
                    raise PublicationError(
                        "Created pull request URL did not match GitHub readback"
                    )
            else:
                viewed = recovered_pull_request
                pull_request_url = viewed["url"]

            # Journal the exact branch, commit, files and PR before waiting for CI.
            # A check-query interruption can therefore resume without recreating or
            # overwriting any remote state.
            pending_checks = {
                "status": "pending",
                "waitExitCode": 8,
                "timedOut": True,
                "requiredChecks": list(required_checks),
                "checks": [],
            }
            published.append(
                _repository_record(
                    repository,
                    commit_sha,
                    viewed,
                    pending_checks,
                )
            )
            _write_evidence(
                progress_destination,
                _publication_progress(
                    plan,
                    published,
                    required_checks=required_checks,
                    context_verification=context_verification,
                    started_at=started_at,
                ),
            )
            published[-1] = _repository_record(
                repository,
                commit_sha,
                viewed,
                _checks(
                    repository.repository,
                    pull_request_url,
                    gh_executable=executable,
                    timeout_seconds=check_timeout_seconds,
                    required_checks=required_checks,
                ),
            )
            _write_evidence(
                progress_destination,
                _publication_progress(
                    plan,
                    published,
                    required_checks=required_checks,
                    context_verification=context_verification,
                    started_at=started_at,
                ),
            )

    # A fully resumed run also refreshes the journal's context and checks.
    _write_evidence(
        progress_destination,
        _publication_progress(
            plan,
            published,
            required_checks=required_checks,
            context_verification=context_verification,
            started_at=started_at,
        ),
    )
    overall = (
        "passed"
        if published and all(item["checks"]["status"] == "passed" for item in published)
        else "failed"
        if any(item["checks"]["status"] == "failed" for item in published)
        else "pending"
    )
    evidence = {
        "schemaVersion": 1,
        "campaignManifestSha256": plan.manifest_sha256,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
        "status": overall,
        "mergePolicy": "manual-only",
        "autoMergeEnabled": False,
        "requiredChecks": list(required_checks),
        "dataHubContextVerification": context_verification.to_dict(),
        "repositories": published,
    }
    current_manifest, current_manifest_sha256 = _manifest(plan.bundle)
    if current_manifest_sha256 != plan.manifest_sha256:
        raise PublicationError("Campaign manifest changed during GitHub publication")
    evidence = validate_publication_evidence(
        current_manifest,
        evidence,
        campaign_manifest_sha256=plan.manifest_sha256,
    )
    _write_evidence(evidence_destination, evidence)
    _refresh_bundle_replay(plan.bundle)
    return evidence

def _verify_published_repository(
    plan: PublicationPlan,
    repository: RepositoryPlan,
    root: Path,
    *,
    gh_executable: str,
) -> tuple[str, dict[str, Any]]:
    checkout = root / repository.label
    _run(
        (
            gh_executable,
            "repo",
            "clone",
            repository.repository,
            checkout,
            "--",
            "--branch",
            repository.head_branch,
        )
    )
    _run(("git", "fetch", "origin", repository.base_branch), cwd=checkout)
    commit_sha = _verify_branch_checkout(repository, checkout)
    pull_request = _find_campaign_pull_request(
        repository,
        gh_executable=gh_executable,
        allow_missing=False,
    )
    return commit_sha, pull_request

def refresh_publication(
    plan: PublicationPlan,
    *,
    approved_manifest_sha256: str,
    required_checks: Sequence[str],
    evidence_path: str | Path | None = None,
    gh_executable: str = "gh",
    check_timeout_seconds: int = 300,
    context_verification: ContextVerification | None = None,
) -> dict[str, Any]:
    """Re-verify existing campaign branches and refresh their check evidence."""
    if approved_manifest_sha256 != plan.manifest_sha256:
        raise ValueError(
            "Approval hash does not match the current campaign manifest; rerun the dry-run"
        )
    if not required_checks:
        raise ValueError("At least one required GitHub check must be named")
    require_fresh_context(plan.datahub_context, context_verification)
    executable = shutil.which(gh_executable)
    if executable is None:
        raise ValueError(f"GitHub CLI executable was not found: {gh_executable}")
    evidence_destination = (
        Path(evidence_path).expanduser().resolve()
        if evidence_path is not None
        else plan.bundle / "publication.json"
    )
    refreshed: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="ripple-proof-pr-refresh-") as temp_dir:
        root = Path(temp_dir).resolve()
        for repository in plan.repositories:
            commit_sha, pull_request = _verify_published_repository(
                plan,
                repository,
                root,
                gh_executable=executable,
            )
            refreshed.append(
                {
                    "label": repository.label,
                    "repository": repository.repository,
                    "baseBranch": repository.base_branch,
                    "headBranch": repository.head_branch,
                    "commitSha": commit_sha,
                    "files": [
                        {
                            "path": file.source_path.as_posix(),
                            "sha256": file.proposed_sha256,
                        }
                        for file in repository.files
                    ],
                    "autoMergeEnabled": False,
                    "pullRequest": pull_request,
                    "checks": _checks(
                        repository.repository,
                        pull_request["url"],
                        gh_executable=executable,
                        timeout_seconds=check_timeout_seconds,
                        required_checks=required_checks,
                    ),
                }
            )
    overall = (
        "passed"
        if refreshed and all(item["checks"]["status"] == "passed" for item in refreshed)
        else "failed"
        if any(item["checks"]["status"] == "failed" for item in refreshed)
        else "pending"
    )
    evidence = {
        "schemaVersion": 1,
        "campaignManifestSha256": plan.manifest_sha256,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
        "refreshedAt": datetime.now(timezone.utc).isoformat(),
        "verificationMode": "fresh-github",
        "status": overall,
        "mergePolicy": "manual-only",
        "autoMergeEnabled": False,
        "requiredChecks": list(required_checks),
        "dataHubContextVerification": context_verification.to_dict(),
        "repositories": refreshed,
    }
    current_manifest, current_manifest_sha256 = _manifest(plan.bundle)
    if current_manifest_sha256 != plan.manifest_sha256:
        raise PublicationError("Campaign manifest changed during GitHub verification")
    evidence = validate_publication_evidence(
        current_manifest,
        evidence,
        campaign_manifest_sha256=plan.manifest_sha256,
    )
    _write_evidence(evidence_destination, evidence)
    _refresh_bundle_replay(plan.bundle)
    return evidence


def _refresh_bundle_replay(bundle: Path) -> None:
    from .replay import load_campaign, render_replay_html

    replay_path = bundle / "campaign.html"
    temporary = replay_path.with_name(f".{replay_path.name}.tmp")
    try:
        temporary.write_text(
            render_replay_html((load_campaign(bundle),)),
            encoding="utf-8",
            newline="\n",
        )
        temporary.replace(replay_path)
    finally:
        temporary.unlink(missing_ok=True)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare or publish an approved RippleProof campaign as GitHub pull requests."
    )
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--repo-map", action="append", default=[])
    parser.add_argument("--base-branch", default="main")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--approve-manifest-sha256")
    parser.add_argument("--required-check", action="append", default=[])
    parser.add_argument("--evidence")
    parser.add_argument("--gh-executable", default="gh")
    parser.add_argument("--check-timeout", type=int, default=300)
    args = parser.parse_args(argv)
    if args.publish and args.refresh:
        parser.error("--publish and --refresh are mutually exclusive")
    if (args.publish or args.refresh) and not args.approve_manifest_sha256:
        parser.error("--publish/--refresh requires --approve-manifest-sha256 from a dry-run")
    if (args.publish or args.refresh) and not args.required_check:
        parser.error("--publish/--refresh requires at least one --required-check")
    if not (args.publish or args.refresh) and args.approve_manifest_sha256:
        parser.error("--approve-manifest-sha256 is only valid with --publish or --refresh")
    if args.check_timeout < 1:
        parser.error("--check-timeout must be at least 1 second")
    return args


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    try:
        plan = prepare_publication(
            args.bundle,
            parse_repository_maps(args.repo_map),
            base_branch=args.base_branch,
        )
        if not (args.publish or args.refresh):
            print(json.dumps(plan.to_dict(), indent=2))
            return
        context_verification = asyncio.run(verify_fresh_context(plan.datahub_context))
        operation = refresh_publication if args.refresh else publish_publication
        evidence = operation(
            plan,
            approved_manifest_sha256=args.approve_manifest_sha256,
            required_checks=args.required_check,
            evidence_path=args.evidence,
            gh_executable=args.gh_executable,
            check_timeout_seconds=args.check_timeout,
            context_verification=context_verification,
        )
    except (ValueError, PublicationError) as exc:
        print(f"RippleProof could not publish the campaign: {exc}")
        raise SystemExit(2) from exc
    print(json.dumps(evidence, indent=2))
    if evidence["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
