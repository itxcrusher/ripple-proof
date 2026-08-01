import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lineage_agent.context_guard import (
    ContextVerification,
    canonical_context_snapshot,
    context_manifest_record,
)
from lineage_agent.github_publication import (
    CommandResult,
    PublicationError,
    _checks,
    parse_repository_maps,
    prepare_publication,
    publish_publication,
)


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"


def matching_context(plan) -> ContextVerification:
    digest = plan.datahub_context.fingerprint_sha256
    return ContextVerification(digest, digest, (), "2026-07-22T00:00:00+00:00")


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
                return checkout, f"commit-{repository.label}"

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
            checks.return_value = {"status": "passed", "checks": []}
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
            )

            with self.assertRaisesRegex(ValueError, "context drifted"):
                publish_publication(
                    plan,
                    approved_manifest_sha256=plan.manifest_sha256,
                    required_checks=("build",),
                    context_verification=verification,
                )

            preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
