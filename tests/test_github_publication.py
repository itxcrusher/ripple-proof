import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lineage_agent.context_guard import (
    ContextVerification,
    LineageFreshnessProof,
    canonical_context_snapshot,
    context_manifest_record,
)
from lineage_agent.github_publication import (
    CommandResult,
    ExistingBranchError,
    PublicationError,
    _checks,
    _recover_unjournaled_repository,
    _verify_branch_checkout,
    parse_repository_maps,
    prepare_publication,
    publish_publication,
    refresh_publication,
)
from lineage_agent.lineage_freshness import CACHE_BYPASS_METHOD


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"


def matching_context(plan) -> ContextVerification:
    digest = plan.datahub_context.fingerprint_sha256
    return ContextVerification(
        digest,
        digest,
        (),
        "2026-07-22T00:00:00+00:00",
        lineage_freshness=matching_lineage(),
    )


def matching_lineage() -> LineageFreshnessProof:
    return LineageFreshnessProof(
        CACHE_BYPASS_METHOD,
        True,
        "a" * 64,
        "a" * 64,
        "b" * 64,
        "b" * 64,
    )


def write_bundle(root: Path, *, ready: bool = True) -> Path:
    bundle = root / "bundle"
    proposed = bundle / "proposed" / "warehouse" / "models" / "orders.sql"
    proposed.parent.mkdir(parents=True)
    proposed_bytes = b"select customer_id from source\n"
    proposed.write_bytes(proposed_bytes)
    manifest = {
        "schemaVersion": 7,
        "source": {
            "urn": SOURCE_URN,
            "dataset": "analytics.orders",
            "rename": {"oldColumn": "user_id", "newColumn": "customer_id"},
            "owners": [],
        },
        "downstreamColumnMode": "propagate",
        "readyForReview": ready,
        "dbtValidation": {"status": "passed"},
        "validations": [{"id": "patch-integrity", "status": "passed"}],
        "downstreamPatches": [
            {
                "repository": "warehouse",
                "sourcePath": "models/orders.sql",
                "proposedPath": "proposed/warehouse/models/orders.sql",
                "sourceSha256": hashlib.sha256(b"select user_id from source\n").hexdigest(),
                "proposedSha256": hashlib.sha256(proposed_bytes).hexdigest(),
            }
        ],
        "contractPatches": [],
        "lineageAssets": [],
    }
    snapshot = canonical_context_snapshot(
        source_urn=SOURCE_URN,
        source_column="user_id",
        max_hops=2,
        schema_fields=("user_id", "order_total"),
        source_owners=(),
        downstream=(),
    )
    manifest["dataHubContext"] = {
        "capture": "live-mcp",
        "maxHops": 2,
        "fingerprint": context_manifest_record(snapshot),
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return bundle


class GitHubPublicationTests(unittest.TestCase):
    @patch("lineage_agent.github_publication.time.sleep")
    @patch("lineage_agent.github_publication._run")
    def test_check_poll_waits_for_delayed_workflow_registration(self, run, sleep):
        run.side_effect = [
            CommandResult(("gh",), 1, "[]", ""),
            CommandResult(
                ("gh",),
                0,
                json.dumps(
                    [
                        {
                            "name": "build",
                            "state": "SUCCESS",
                            "bucket": "pass",
                            "link": "https://github.com/itxcrusher/demo/actions/runs/1",
                            "workflow": "dbt validation",
                        }
                    ]
                ),
                "",
            ),
        ]

        result = _checks(
            "itxcrusher/demo",
            "https://github.com/itxcrusher/demo/pull/1",
            gh_executable="gh",
            timeout_seconds=30,
            required_checks=("build",),
        )

        self.assertEqual(result["status"], "passed")
        sleep.assert_called_once()

    def test_dry_run_requires_validated_campaign_and_returns_approval_hash(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))

            plan = prepare_publication(
                bundle,
                {"warehouse": "itxcrusher/ripple-proof-dbt-analytics"},
            )

            self.assertEqual(
                plan.manifest_sha256,
                hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest(),
            )
            self.assertEqual(plan.repositories[0].head_branch, f"ripple-proof/propagate-{plan.manifest_sha256[:12]}")
            self.assertEqual(plan.repositories[0].files[0].source_path, Path("models/orders.sql"))
            self.assertIn("human review only", plan.body)

    def test_rejects_campaign_that_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), ready=False)

            with self.assertRaisesRegex(ValueError, "not ready"):
                prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})

    def test_repository_mapping_is_strict(self):
        self.assertEqual(
            parse_repository_maps(("warehouse=itxcrusher/demo",)),
            {"warehouse": "itxcrusher/demo"},
        )
        with self.assertRaisesRegex(ValueError, "owner/repository"):
            parse_repository_maps(("warehouse=https://github.com/itxcrusher/demo",))

    def test_rejects_unsafe_campaign_repository_label(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["downstreamPatches"][0]["repository"] = "../warehouse"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "Unsafe campaign repository label"):
                prepare_publication(bundle, {"../warehouse": "itxcrusher/demo"})

    def test_publish_requires_exact_manifest_approval(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})

            with self.assertRaisesRegex(ValueError, "Approval hash"):
                publish_publication(
                    plan,
                    approved_manifest_sha256="0" * 64,
                )

    @patch("lineage_agent.github_publication._refresh_bundle_replay")
    @patch("lineage_agent.github_publication._checks")
    @patch("lineage_agent.github_publication._run")
    @patch("lineage_agent.github_publication._preflight_repository")
    @patch("lineage_agent.github_publication.shutil.which", return_value="C:/tools/gh.exe")
    def test_preflights_every_repository_before_the_first_push(
        self,
        _which,
        preflight,
        run,
        checks,
        _refresh_replay,
    ):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root)
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            second = dict(manifest["downstreamPatches"][0])
            second["repository"] = "finance"
            second["proposedPath"] = "proposed/finance/models/orders.sql"
            second_path = bundle / second["proposedPath"]
            second_path.parent.mkdir(parents=True)
            second_path.write_bytes((bundle / manifest["downstreamPatches"][0]["proposedPath"]).read_bytes())
            manifest["downstreamPatches"].append(second)
            (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            plan = prepare_publication(
                bundle,
                {
                    "warehouse": "itxcrusher/analytics-demo",
                    "finance": "itxcrusher/finance-demo",
                },
            )
            call_order = []

            def fake_preflight(_plan, repository, temp_root, **_kwargs):
                checkout = temp_root / repository.label
                checkout.mkdir()
                call_order.append(f"preflight:{repository.label}")
                return checkout, (
                    "a" * 40 if repository.label == "finance" else "b" * 40
                )

            def fake_run(command, **_kwargs):
                values = tuple(str(value) for value in command)
                if values[:2] == ("git", "push"):
                    call_order.append("push")
                    return CommandResult(values, 0, "", "")
                if values[1:3] == ("pr", "create"):
                    repository = values[values.index("--repo") + 1]
                    return CommandResult(values, 0, f"https://github.com/{repository}/pull/1\n", "")
                if values[1:3] == ("pr", "view"):
                    url = values[3]
                    return CommandResult(
                        values,
                        0,
                        json.dumps(
                            {
                                "number": 1,
                                "url": url,
                                "state": "OPEN",
                                "headRefName": plan.repositories[0].head_branch,
                                "baseRefName": "main",
                            }
                        ),
                        "",
                    )
                raise AssertionError(values)

            preflight.side_effect = fake_preflight
            run.side_effect = fake_run
            checks.return_value = {
                "status": "passed",
                "requiredChecks": ["build"],
                "timedOut": False,
                "waitExitCode": 0,
                "checks": [
                    {"name": "build", "bucket": "pass", "state": "SUCCESS"}
                ],
            }
            evidence_path = root / "publication.json"

            evidence = publish_publication(
                plan,
                approved_manifest_sha256=plan.manifest_sha256,
                evidence_path=evidence_path,
                required_checks=("build",),
                context_verification=matching_context(plan),
            )

            first_push = call_order.index("push")
            self.assertEqual(call_order[:first_push], ["preflight:finance", "preflight:warehouse"])
            self.assertEqual(evidence["status"], "passed")
            self.assertFalse(evidence["autoMergeEnabled"])
            self.assertEqual(len(evidence["repositories"]), 2)
            self.assertTrue(
                all(repository["files"] for repository in evidence["repositories"])
            )
            self.assertTrue(
                all(
                    len(repository["commitSha"]) == 40
                    for repository in evidence["repositories"]
                )
            )
            self.assertTrue(
                all(not repository["autoMergeEnabled"] for repository in evidence["repositories"])
            )
            self.assertEqual(
                json.loads(evidence_path.read_text(encoding="utf-8"))["campaignManifestSha256"],
                plan.manifest_sha256,
            )

    @patch("lineage_agent.github_publication._run")
    @patch("lineage_agent.github_publication._preflight_repository")
    @patch("lineage_agent.github_publication.shutil.which", return_value="C:/tools/gh.exe")
    def test_preflight_failure_happens_before_any_push(self, _which, preflight, run):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            preflight.side_effect = PublicationError("remote source drifted")

            with self.assertRaisesRegex(PublicationError, "drifted"):
                publish_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    required_checks=("build",),
                    context_verification=matching_context(plan),
                )

            run.assert_not_called()

    @patch("lineage_agent.github_publication._preflight_repository")
    @patch("lineage_agent.github_publication.shutil.which", return_value="C:/tools/gh.exe")
    def test_context_drift_fails_before_repository_preflight(self, _which, preflight):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            verification = ContextVerification(
                plan.datahub_context.fingerprint_sha256,
                "f" * 64,
                ("source schema changed (added: account_id; removed: none)",),
                "2026-07-22T00:00:00+00:00",
                lineage_freshness=matching_lineage(),
            )

            with self.assertRaisesRegex(ValueError, "context drifted"):
                publish_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    required_checks=("build",),
                    context_verification=verification,
                )

            preflight.assert_not_called()




    def test_failure_after_push_recovers_branch_and_creates_missing_pr(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root)
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            repository = plan.repositories[0]
            evidence_path = root / "publication.json"
            commit_sha = "a" * 40

            def preflight(_plan, _repository, temp_root, **_kwargs):
                checkout = temp_root / "warehouse"
                checkout.mkdir()
                return checkout, commit_sha

            def interrupted(command, **_kwargs):
                values = tuple(str(value) for value in command)
                if values[:2] == ("git", "push"):
                    return CommandResult(values, 0, "", "")
                if values[1:3] == ("pr", "create"):
                    raise PublicationError("simulated failure after push")
                raise AssertionError(values)

            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository",
                    side_effect=preflight,
                ),
                patch("lineage_agent.github_publication._run", side_effect=interrupted),
            ):
                with self.assertRaisesRegex(PublicationError, "after push"):
                    publish_publication(
                        plan,
                        approved_manifest_sha256=plan.manifest_sha256,
                        evidence_path=evidence_path,
                        required_checks=("build",),
                        context_verification=matching_context(plan),
                    )

            self.assertFalse((root / "publication.progress.json").exists())
            pull_request = {
                "number": 1,
                "url": "https://github.com/itxcrusher/demo/pull/1",
                "state": "OPEN",
                "headRefName": repository.head_branch,
                "baseRefName": repository.base_branch,
                "autoMergeRequest": None,
            }
            operations = []

            def resumed(command, **_kwargs):
                values = tuple(str(value) for value in command)
                operations.append(
                    values[:2] if values[0] == "git" else values[1:3]
                )
                if values[:2] == ("git", "push"):
                    raise AssertionError("recovery must never overwrite the branch")
                if values[1:3] == ("pr", "create"):
                    return CommandResult(
                        values,
                        0,
                        "https://github.com/itxcrusher/demo/pull/1\n",
                        "",
                    )
                if values[1:3] == ("pr", "view"):
                    return CommandResult(values, 0, json.dumps(pull_request), "")
                raise AssertionError(values)

            passing = {
                "status": "passed",
                "requiredChecks": ["build"],
                "timedOut": False,
                "waitExitCode": 0,
                "checks": [{"name": "build", "bucket": "pass", "state": "SUCCESS"}],
            }
            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository",
                    side_effect=ExistingBranchError("exists"),
                ),
                patch(
                    "lineage_agent.github_publication._recover_unjournaled_repository",
                    return_value=(root / "recovered", commit_sha, None),
                ),
                patch("lineage_agent.github_publication._run", side_effect=resumed),
                patch(
                    "lineage_agent.github_publication._checks",
                    return_value=passing,
                ),
                patch("lineage_agent.github_publication._refresh_bundle_replay"),
            ):
                evidence = publish_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    evidence_path=evidence_path,
                    required_checks=("build",),
                    context_verification=matching_context(plan),
                )

            self.assertEqual(evidence["status"], "passed")
            self.assertNotIn(("git", "push"), operations)
            self.assertIn(("pr", "create"), operations)

    def test_failure_after_pr_create_adopts_exact_pr_without_recreating(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root)
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            repository = plan.repositories[0]
            evidence_path = root / "publication.json"
            commit_sha = "a" * 40
            pull_request = {
                "number": 1,
                "url": "https://github.com/itxcrusher/demo/pull/1",
                "state": "OPEN",
                "headRefName": repository.head_branch,
                "baseRefName": repository.base_branch,
                "autoMergeRequest": None,
            }

            def preflight(_plan, _repository, temp_root, **_kwargs):
                checkout = temp_root / "warehouse"
                checkout.mkdir()
                return checkout, commit_sha

            def interrupted(command, **_kwargs):
                values = tuple(str(value) for value in command)
                if values[:2] == ("git", "push"):
                    return CommandResult(values, 0, "", "")
                if values[1:3] == ("pr", "create"):
                    return CommandResult(
                        values,
                        0,
                        "https://github.com/itxcrusher/demo/pull/1\n",
                        "",
                    )
                if values[1:3] == ("pr", "view"):
                    raise PublicationError("simulated failure after PR create")
                raise AssertionError(values)

            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository",
                    side_effect=preflight,
                ),
                patch("lineage_agent.github_publication._run", side_effect=interrupted),
            ):
                with self.assertRaisesRegex(PublicationError, "after PR create"):
                    publish_publication(
                        plan,
                        approved_manifest_sha256=plan.manifest_sha256,
                        evidence_path=evidence_path,
                        required_checks=("build",),
                        context_verification=matching_context(plan),
                    )

            passing = {
                "status": "passed",
                "requiredChecks": ["build"],
                "timedOut": False,
                "waitExitCode": 0,
                "checks": [{"name": "build", "bucket": "pass", "state": "SUCCESS"}],
            }
            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository",
                    side_effect=ExistingBranchError("exists"),
                ),
                patch(
                    "lineage_agent.github_publication._recover_unjournaled_repository",
                    return_value=(root / "recovered", commit_sha, pull_request),
                ),
                patch("lineage_agent.github_publication._run") as run,
                patch(
                    "lineage_agent.github_publication._checks",
                    return_value=passing,
                ),
                patch("lineage_agent.github_publication._refresh_bundle_replay"),
            ):
                evidence = publish_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    evidence_path=evidence_path,
                    required_checks=("build",),
                    context_verification=matching_context(plan),
                )

            self.assertEqual(evidence["status"], "passed")
            run.assert_not_called()

    def test_unjournaled_recovery_refuses_changed_closed_wrong_or_multiple_prs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            repository = plan.repositories[0]
            root = Path(temp_dir) / "work"
            root.mkdir()
            base_pr = {
                "number": 1,
                "url": "https://github.com/itxcrusher/demo/pull/1",
                "state": "OPEN",
                "headRefName": repository.head_branch,
                "baseRefName": repository.base_branch,
                "autoMergeRequest": None,
            }

            def run_with_prs(prs):
                def fake_run(command, **_kwargs):
                    values = tuple(str(value) for value in command)
                    if values[1:3] == ("pr", "list"):
                        return CommandResult(values, 0, json.dumps(prs), "")
                    if values[:2] == ("git", "rev-parse"):
                        return CommandResult(values, 0, "a" * 40 + "\n", "")
                    return CommandResult(values, 0, "", "")
                return fake_run

            with (
                patch(
                    "lineage_agent.github_publication._verify_branch_checkout",
                    side_effect=PublicationError("branch differs"),
                ),
                patch(
                    "lineage_agent.github_publication._run",
                    side_effect=run_with_prs([]),
                ),
            ):
                with self.assertRaisesRegex(PublicationError, "branch differs"):
                    _recover_unjournaled_repository(
                        plan, repository, root, gh_executable="gh"
                    )

            cases = {
                "closed": [{**base_pr, "state": "CLOSED"}],
                "wrong": [{**base_pr, "baseRefName": "release"}],
                "multiple": [base_pr, {**base_pr, "number": 2, "url":
                    "https://github.com/itxcrusher/demo/pull/2"}],
            }
            for label, pull_requests in cases.items():
                with self.subTest(label=label):
                    with (
                        patch(
                            "lineage_agent.github_publication._verify_branch_checkout",
                            return_value="a" * 40,
                        ),
                        patch(
                            "lineage_agent.github_publication._run",
                            side_effect=run_with_prs(pull_requests),
                        ),
                    ):
                        with self.assertRaises(PublicationError):
                            _recover_unjournaled_repository(
                                plan, repository, root, gh_executable="gh"
                            )

    def test_branch_checkout_verifies_complete_diff_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            repository = plan.repositories[0]
            checkout = Path(temp_dir) / "checkout"
            destination = checkout / repository.files[0].source_path
            destination.parent.mkdir(parents=True)
            destination.write_bytes(
                (bundle / repository.files[0].proposed_path).read_bytes()
            )

            with patch(
                "lineage_agent.github_publication._run",
                side_effect=[
                    CommandResult(
                        ("git",),
                        0,
                        repository.files[0].source_path.as_posix() + "\n",
                        "",
                    ),
                    CommandResult(("git",), 0, "a" * 40 + "\n", ""),
                ],
            ):
                self.assertEqual(
                    _verify_branch_checkout(repository, checkout), "a" * 40
                )

            destination.write_text("tampered\n", encoding="utf-8")
            with patch(
                "lineage_agent.github_publication._run",
                return_value=CommandResult(
                    ("git",),
                    0,
                    repository.files[0].source_path.as_posix() + "\n",
                    "",
                ),
            ):
                with self.assertRaisesRegex(PublicationError, "does not match"):
                    _verify_branch_checkout(repository, checkout)

            with patch(
                "lineage_agent.github_publication._run",
                return_value=CommandResult(
                    ("git",),
                    0,
                    repository.files[0].source_path.as_posix()
                    + "\nunexpected.sql\n",
                    "",
                ),
            ):
                with self.assertRaisesRegex(PublicationError, "differs"):
                    _verify_branch_checkout(repository, checkout)

    def test_refresh_marks_an_exact_fresh_github_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            repository = plan.repositories[0]
            pull_request = {
                "number": 1,
                "url": "https://github.com/itxcrusher/demo/pull/1",
                "state": "OPEN",
                "headRefName": repository.head_branch,
                "baseRefName": repository.base_branch,
                "autoMergeRequest": None,
            }
            passing = {
                "status": "passed",
                "requiredChecks": ["build"],
                "timedOut": False,
                "waitExitCode": 0,
                "checks": [{"name": "build", "bucket": "pass", "state": "SUCCESS"}],
            }
            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._verify_published_repository",
                    return_value=("a" * 40, pull_request),
                ),
                patch(
                    "lineage_agent.github_publication._checks",
                    return_value=passing,
                ),
                patch("lineage_agent.github_publication._refresh_bundle_replay"),
            ):
                evidence = refresh_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    required_checks=("build",),
                    context_verification=matching_context(plan),
                )

            self.assertEqual(evidence["verificationMode"], "fresh-github")
            self.assertTrue(evidence["refreshedAt"])
            stored = json.loads(
                (bundle / "publication.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stored, evidence)

    def test_mid_batch_failure_journals_and_resumes_exact_remote_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root)
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            second = dict(manifest["downstreamPatches"][0])
            second["repository"] = "finance"
            second["proposedPath"] = "proposed/finance/models/orders.sql"
            second_path = bundle / second["proposedPath"]
            second_path.parent.mkdir(parents=True)
            second_path.write_bytes(
                (bundle / manifest["downstreamPatches"][0]["proposedPath"]).read_bytes()
            )
            manifest["downstreamPatches"].append(second)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            plan = prepare_publication(
                bundle,
                {
                    "finance": "itxcrusher/finance-demo",
                    "warehouse": "itxcrusher/warehouse-demo",
                },
            )
            evidence_path = root / "publication.json"
            commits = {"finance": "a" * 40, "warehouse": "b" * 40}

            def fake_preflight(_plan, repository, temp_root, **_kwargs):
                checkout = temp_root / repository.label
                checkout.mkdir()
                return checkout, commits[repository.label]

            def viewed(repository, number):
                return {
                    "number": number,
                    "url": f"https://github.com/{repository.repository}/pull/{number}",
                    "state": "OPEN",
                    "headRefName": repository.head_branch,
                    "baseRefName": repository.base_branch,
                    "autoMergeRequest": None,
                }

            def first_run(command, **kwargs):
                values = tuple(str(value) for value in command)
                if values[:2] == ("git", "push"):
                    if Path(kwargs["cwd"]).name == "warehouse":
                        raise PublicationError("simulated second repository failure")
                    return CommandResult(values, 0, "", "")
                if values[1:3] == ("pr", "create"):
                    repository = values[values.index("--repo") + 1]
                    return CommandResult(
                        values,
                        0,
                        f"https://github.com/{repository}/pull/1\n",
                        "",
                    )
                if values[1:3] == ("pr", "view"):
                    repository = plan.repositories[0]
                    return CommandResult(values, 0, json.dumps(viewed(repository, 1)), "")
                raise AssertionError(values)

            passing = {
                "status": "passed",
                "requiredChecks": ["build"],
                "timedOut": False,
                "waitExitCode": 0,
                "checks": [{"name": "build", "bucket": "pass", "state": "SUCCESS"}],
            }
            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository",
                    side_effect=fake_preflight,
                ),
                patch("lineage_agent.github_publication._run", side_effect=first_run),
                patch(
                    "lineage_agent.github_publication._checks",
                    return_value=passing,
                ),
                patch("lineage_agent.github_publication._refresh_bundle_replay"),
            ):
                with self.assertRaisesRegex(
                    PublicationError, "second repository failure"
                ):
                    publish_publication(
                        plan,
                        approved_manifest_sha256=plan.manifest_sha256,
                        evidence_path=evidence_path,
                        required_checks=("build",),
                        context_verification=matching_context(plan),
                    )

            progress_path = root / "publication.progress.json"
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(progress["campaignManifestSha256"], plan.manifest_sha256)
            self.assertEqual(progress["completedRepositories"], ["finance"])
            self.assertEqual(progress["remainingRepositories"], ["warehouse"])
            self.assertEqual(progress["repositories"][0]["commitSha"], commits["finance"])

            second_preflights = []

            def resume_preflight(_plan, repository, temp_root, **_kwargs):
                second_preflights.append(repository.label)
                return fake_preflight(_plan, repository, temp_root, **_kwargs)

            def resume_verify(_plan, repository, _root, **_kwargs):
                self.assertEqual(repository.label, "finance")
                return commits["finance"], viewed(repository, 1)

            def second_run(command, **_kwargs):
                values = tuple(str(value) for value in command)
                if values[:2] == ("git", "push"):
                    return CommandResult(values, 0, "", "")
                if values[1:3] == ("pr", "create"):
                    repository = values[values.index("--repo") + 1]
                    return CommandResult(
                        values,
                        0,
                        f"https://github.com/{repository}/pull/2\n",
                        "",
                    )
                if values[1:3] == ("pr", "view"):
                    repository = plan.repositories[1]
                    return CommandResult(values, 0, json.dumps(viewed(repository, 2)), "")
                raise AssertionError(values)

            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._verify_published_repository",
                    side_effect=resume_verify,
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository",
                    side_effect=resume_preflight,
                ),
                patch("lineage_agent.github_publication._run", side_effect=second_run),
                patch(
                    "lineage_agent.github_publication._checks",
                    return_value=passing,
                ),
                patch("lineage_agent.github_publication._refresh_bundle_replay"),
            ):
                evidence = publish_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    evidence_path=evidence_path,
                    required_checks=("build",),
                    context_verification=matching_context(plan),
                )

            self.assertEqual(second_preflights, ["warehouse"])
            self.assertEqual(evidence["status"], "passed")
            self.assertEqual(
                [item["label"] for item in evidence["repositories"]],
                ["finance", "warehouse"],
            )
            completed = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "completed")

            # A later idempotent invocation fails before any new preflight/push if
            # the recorded PR is now closed or otherwise no longer exact.
            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._verify_published_repository",
                    side_effect=PublicationError("campaign pull request is closed"),
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository"
                ) as preflight,
                patch("lineage_agent.github_publication._run") as run,
            ):
                with self.assertRaisesRegex(PublicationError, "closed"):
                    publish_publication(
                        plan,
                        approved_manifest_sha256=plan.manifest_sha256,
                        evidence_path=evidence_path,
                        required_checks=("build",),
                        context_verification=matching_context(plan),
                    )
                preflight.assert_not_called()
                run.assert_not_called()

    def test_resume_rejects_changed_recorded_commit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root)
            plan = prepare_publication(bundle, {"warehouse": "itxcrusher/demo"})
            evidence_path = root / "publication.json"
            repository = plan.repositories[0]
            pull_request = {
                "number": 1,
                "url": "https://github.com/itxcrusher/demo/pull/1",
                "state": "OPEN",
                "headRefName": repository.head_branch,
                "baseRefName": repository.base_branch,
                "autoMergeRequest": None,
            }
            progress = {
                "schemaVersion": 1,
                "campaignManifestSha256": plan.manifest_sha256,
                "status": "completed",
                "mergePolicy": "manual-only",
                "autoMergeEnabled": False,
                "requiredChecks": ["build"],
                "startedAt": "2026-07-22T00:00:00+00:00",
                "updatedAt": "2026-07-22T00:00:00+00:00",
                "dataHubContextVerification": matching_context(plan).to_dict(),
                "completedRepositories": ["warehouse"],
                "remainingRepositories": [],
                "repositories": [
                    {
                        "label": "warehouse",
                        "repository": "itxcrusher/demo",
                        "baseBranch": repository.base_branch,
                        "headBranch": repository.head_branch,
                        "commitSha": "a" * 40,
                        "files": [
                            {
                                "path": file.source_path.as_posix(),
                                "sha256": file.proposed_sha256,
                            }
                            for file in repository.files
                        ],
                        "autoMergeEnabled": False,
                        "pullRequest": pull_request,
                        "checks": {},
                    }
                ],
            }
            (root / "publication.progress.json").write_text(
                json.dumps(progress), encoding="utf-8"
            )
            with (
                patch(
                    "lineage_agent.github_publication.shutil.which",
                    return_value="C:/tools/gh.exe",
                ),
                patch(
                    "lineage_agent.github_publication._verify_published_repository",
                    return_value=("b" * 40, pull_request),
                ),
                patch(
                    "lineage_agent.github_publication._preflight_repository"
                ) as preflight,
            ):
                with self.assertRaisesRegex(PublicationError, "no longer matches"):
                    publish_publication(
                        plan,
                        approved_manifest_sha256=plan.manifest_sha256,
                        evidence_path=evidence_path,
                        required_checks=("build",),
                        context_verification=matching_context(plan),
                    )
                preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
