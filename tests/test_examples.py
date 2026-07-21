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


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeneratedExampleTests(unittest.TestCase):
    def test_manifest_hashes_match_checked_in_models(self):
        manifest = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
        patch = manifest["downstreamPatches"][0]

        self.assertEqual(manifest["schemaVersion"], 3)
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

        self.assertEqual(manifest["schemaVersion"], 3)
        self.assertEqual(manifest["downstreamColumnMode"], "propagate")
        self.assertEqual(len(manifest["downstreamPatches"]), 2)
        self.assertEqual(len(manifest["contractPatches"]), 2)
        self.assertTrue(manifest["readyForReview"])

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


if __name__ == "__main__":
    unittest.main()
