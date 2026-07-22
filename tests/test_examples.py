import hashlib
import json
import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SOURCE = PROJECT_ROOT / "examples/dbt_project/models/orders_daily.sql"
EXAMPLE_PROPOSED = (
    PROJECT_ROOT
    / "examples/generated/proposed/dbt_project/models/orders_daily.sql"
)
EXAMPLE_MANIFEST = PROJECT_ROOT / "examples/generated/manifest.json"
EXAMPLE_PATCH = (
    PROJECT_ROOT
    / "examples/generated/patches/dbt_project/models/orders_daily.sql.patch"
)
PROPAGATE_MANIFEST = PROJECT_ROOT / "examples/generated-propagate/manifest.json"
PROPAGATE_PUBLICATION = PROJECT_ROOT / "examples/generated-propagate/publication.json"
PROPAGATE_DECISION = PROJECT_ROOT / "examples/generated-propagate/decision.json"
RICH_PRESERVE_MANIFEST = (
    PROJECT_ROOT / "examples/generated-rich-preserve/manifest.json"
)
RICH_PROPAGATE_MANIFEST = (
    PROJECT_ROOT / "examples/generated-rich-propagate/manifest.json"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeneratedExampleTests(unittest.TestCase):
    def test_richer_preserve_campaign_routes_every_owner_and_builds_cleanly(self):
        manifest = json.loads(RICH_PRESERVE_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 5)
        self.assertEqual(len(manifest["lineageAssets"]), 5)
        self.assertEqual(len(manifest["targetRepositories"]), 5)
        self.assertEqual(len(manifest["reviewAssignments"]), 6)
        self.assertTrue(
            all(
                assignment["routingStatus"] == "assigned"
                for assignment in manifest["reviewAssignments"]
            )
        )
        self.assertEqual(len(manifest["downstreamPatches"]), 1)
        self.assertEqual(len(manifest["protectedByContract"]), 3)
        self.assertEqual(len(manifest["manualReview"]), 1)
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertTrue(manifest["readyForReview"])
        self.assertTrue(
            all(
                project["steps"][-1]["summary"]
                == "PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4"
                for project in manifest["dbtValidation"]["projects"]
            )
        )

    def test_richer_propagate_campaign_fails_closed_on_ambiguous_consumer(self):
        manifest = json.loads(RICH_PROPAGATE_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 5)
        self.assertEqual(len(manifest["downstreamPatches"]), 3)
        self.assertEqual(len(manifest["contractPatches"]), 3)
        ambiguity = next(
            item
            for item in manifest["manualReview"]
            if item["assetName"] == "customer_identity_export"
        )
        self.assertIn("Multiple dbt models match", ambiguity["reason"])
        self.assertIn("dbt_operations_primary", ambiguity["reason"])
        self.assertIn("dbt_operations_shadow", ambiguity["reason"])
        self.assertEqual(manifest["dbtValidation"]["status"], "failed")
        self.assertFalse(manifest["readyForReview"])
        self.assertEqual(
            {
                project["repository"]: project["status"]
                for project in manifest["dbtValidation"]["projects"]
            },
            {
                "dbt_finance_project": "passed",
                "dbt_growth_project": "passed",
                "dbt_operations_primary": "failed",
                "dbt_operations_shadow": "failed",
            },
        )
        self.assertEqual(
            next(
                check
                for check in manifest["validations"]
                if check["id"] == "dbt-build"
            )["status"],
            "failed",
        )

    def test_richer_propagate_patches_apply_to_supported_repositories(self):
        manifest = json.loads(RICH_PROPAGATE_MANIFEST.read_text(encoding="utf-8"))

        for patch in manifest["downstreamPatches"] + manifest["contractPatches"]:
            completed = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    f"--directory=examples/{patch['repository']}",
                    str(
                        (RICH_PROPAGATE_MANIFEST.parent / patch["diffPath"]).relative_to(
                            PROJECT_ROOT
                        )
                    ),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manifest_hashes_match_checked_in_models(self):
        manifest = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
        patch = manifest["downstreamPatches"][0]

        self.assertEqual(manifest["schemaVersion"], 4)
        self.assertEqual(sha256(EXAMPLE_SOURCE), patch["sourceSha256"])
        self.assertEqual(sha256(EXAMPLE_PROPOSED), patch["proposedSha256"])
        self.assertEqual(patch["preservedOutputAliases"], 1)
        self.assertEqual(patch["remainingExecutableOldIdentifiers"], 1)
        self.assertEqual(manifest["downstreamColumnMode"], "preserve")
        self.assertEqual(
            manifest["targetRepositories"],
            ["dbt_project", "dbt_finance_project"],
        )
        self.assertEqual(manifest["contractPatches"], [])
        self.assertEqual(len(manifest["protectedByContract"]), 1)
        self.assertEqual(
            manifest["protectedByContract"][0]["assetName"],
            "customer_revenue",
        )
        self.assertTrue(manifest["readyForReview"])
        self.assertFalse(manifest["safety"]["targetRepositoriesModified"])
        self.assertFalse(manifest["safety"]["sqlExecuted"])
        self.assertFalse(manifest["safety"]["downstreamOutputContractMayChange"])
        self.assertFalse(manifest["safety"]["transitiveConsumersModified"])
        self.assertEqual(manifest["dataHubContext"]["capture"], "live-mcp")
        self.assertEqual(len(manifest["lineageAssets"]), 3)
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertEqual(manifest["dbtValidation"]["dbtVersion"], "1.9.10")
        self.assertTrue(manifest["safety"]["dbtValidationExecuted"])
        self.assertTrue((PROJECT_ROOT / "examples/generated/campaign.html").is_file())

    def test_checked_in_patch_applies_to_example_project(self):
        completed = subprocess.run(
            [
                "git",
                "apply",
                "--check",
                "--directory=examples/dbt_project",
                str(EXAMPLE_PATCH.relative_to(PROJECT_ROOT)),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_propagate_campaign_patches_apply_to_both_example_projects(self):
        manifest = json.loads(PROPAGATE_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 4)
        self.assertEqual(manifest["downstreamColumnMode"], "propagate")
        self.assertEqual(len(manifest["downstreamPatches"]), 2)
        self.assertEqual(len(manifest["contractPatches"]), 2)
        self.assertTrue(manifest["readyForReview"])
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertEqual(
            manifest["dbtValidation"]["projects"][0]["steps"][-1]["summary"],
            "PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4",
        )

        for patch in manifest["downstreamPatches"] + manifest["contractPatches"]:
            completed = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    f"--directory=examples/{patch['repository']}",
                    str(
                        (PROPAGATE_MANIFEST.parent / patch["diffPath"]).relative_to(
                            PROJECT_ROOT
                        )
                    ),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_propagate_publication_is_bound_to_passing_review_prs(self):
        publication = json.loads(PROPAGATE_PUBLICATION.read_text(encoding="utf-8"))

        self.assertEqual(publication["campaignManifestSha256"], sha256(PROPAGATE_MANIFEST))
        self.assertEqual(publication["status"], "passed")
        self.assertEqual(publication["mergePolicy"], "manual-only")
        self.assertFalse(publication["autoMergeEnabled"])
        self.assertEqual(publication["requiredChecks"], ["build"])
        self.assertEqual(len(publication["repositories"]), 2)
        for repository in publication["repositories"]:
            self.assertFalse(repository["autoMergeEnabled"])
            self.assertIsNone(repository["pullRequest"]["autoMergeRequest"])
            self.assertEqual(repository["pullRequest"]["state"], "OPEN")
            self.assertTrue(repository["pullRequest"]["url"].startswith("https://github.com/"))
            self.assertEqual(repository["checks"]["status"], "passed")
            self.assertIn(
                "build",
                [check["name"] for check in repository["checks"]["checks"]],
            )

    def test_propagate_decision_is_bound_to_fresh_datahub_readback(self):
        decision = json.loads(PROPAGATE_DECISION.read_text(encoding="utf-8"))

        self.assertEqual(decision["campaignManifestSha256"], sha256(PROPAGATE_MANIFEST))
        self.assertEqual(decision["decision"], "approved-for-review")
        self.assertEqual(decision["mergePolicy"], "manual-only")
        self.assertTrue(decision["freshSessionReadback"])
        self.assertEqual(set(decision["assets"]), set(decision["verifiedAssets"]))
        self.assertEqual(len(decision["verifiedAssets"]), 4)


if __name__ == "__main__":
    unittest.main()
