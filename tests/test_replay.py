import json
import tempfile
import unittest
from pathlib import Path

from lineage_agent.replay import load_campaign, render_replay_html, write_replay


def write_bundle(root: Path, mode: str) -> Path:
    bundle = root / mode
    patches = bundle / "patches"
    patches.mkdir(parents=True)
    (bundle / "migration.sql").write_text(
        'ALTER TABLE "analytics"."orders" RENAME COLUMN "user_id" TO "customer_id";\n',
        encoding="utf-8",
    )
    (bundle / "impact-report.md").write_text("# Impact\n", encoding="utf-8")
    (patches / "orders.sql.patch").write_text(
        "--- a/orders.sql\n+++ b/orders.sql\n@@ -1 +1 @@\n-user_id\n+customer_id\n",
        encoding="utf-8",
    )
    manifest = {
        "schemaVersion": 3,
        "source": {
            "urn": "urn:source",
            "dataset": "analytics.orders",
            "platform": "postgres",
            "instruction": "rename user_id to customer_id",
            "rename": {"oldColumn": "user_id", "newColumn": "customer_id"},
        },
        "migration": "migration.sql",
        "impactReport": "impact-report.md",
        "downstreamColumnMode": mode,
        "readyForReview": True,
        "targetRepositories": ["warehouse"],
        "lineageAssets": [
            {
                "urn": "urn:orders_daily",
                "name": "orders_daily",
                "type": "DATASET",
                "platform": "dbt",
                "degree": 1,
                "columns": ["user_id"],
                "evidence": "column",
            }
        ],
        "dataHubContext": {
            "capture": "live-mcp",
            "reads": [{"tool": "get_lineage(column)", "detail": "One match."}],
        },
        "downstreamPatches": [
            {
                "assetUrn": "urn:orders_daily",
                "assetName": "orders_daily",
                "repository": "warehouse",
                "sourcePath": "models/orders.sql",
                "diffPath": "patches/orders.sql.patch",
            }
        ],
        "contractPatches": [],
        "protectedByContract": [],
        "manualReview": [],
        "validations": [
            {"id": "patch-integrity", "status": "passed", "detail": "Hashes match."}
        ],
        "safety": {
            "sqlExecuted": False,
            "targetRepositoriesModified": False,
            "humanReviewRequired": True,
            "downstreamOutputContractMayChange": mode == "propagate",
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


class ReplayTests(unittest.TestCase):
    def test_loads_artifacts_and_renders_two_real_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preserve = load_campaign(write_bundle(root, "preserve"))
            propagate = load_campaign(write_bundle(root, "propagate"))

            html = render_replay_html((preserve, propagate))

            self.assertIn("Preserve contract", html)
            self.assertIn("Propagate rename", html)
            self.assertIn("orders_daily", html)
            self.assertIn("customer_id", html)
            self.assertIn("prefers-reduced-motion", html)
            self.assertNotIn("https://fonts.googleapis.com", html)

    def test_rejects_manifest_artifact_that_escapes_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root, "preserve")
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["migration"] = "../outside.sql"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "outside.sql").write_text("select 1", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "escapes its directory"):
                load_campaign(bundle)

    def test_escapes_embedded_script_terminators_from_artifact_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            campaign = load_campaign(write_bundle(root, "preserve"))
            campaign["impactReport"] = "</script><script>alert('unsafe')</script>"

            html = render_replay_html((campaign,))

            self.assertNotIn("</script><script>alert('unsafe')</script>", html)
            self.assertIn("<\\/script><script>alert('unsafe')<\\/script>", html)

    def test_write_is_atomic_and_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            campaign = load_campaign(write_bundle(root, "preserve"))
            output = root / "campaign.html"

            written = write_replay((campaign,), output)

            self.assertEqual(written, output.resolve())
            self.assertTrue(output.is_file())
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                write_replay((campaign,), output)
            write_replay((campaign,), output, overwrite=True)
            self.assertIn(
                "BlastRadius campaign review",
                output.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
