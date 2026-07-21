import json
import tempfile
import unittest
from pathlib import Path

from lineage_agent.dbt_validation import validate_bundle, validate_dbt_campaign


class DbtValidationTests(unittest.TestCase):
    def test_missing_executable_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "warehouse"
            project.mkdir()

            result = validate_dbt_campaign(
                {"warehouse": project},
                (),
                dbt_executable=root / "does-not-exist",
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.projects[0].steps[0].exit_code, 127)
            self.assertIn("not found", result.projects[0].steps[0].summary)

    def test_existing_bundle_rejects_proposed_file_outside_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "warehouse"
            project.mkdir()
            (project / "dbt_project.yml").write_text(
                "name: warehouse\nprofile: warehouse\n",
                encoding="utf-8",
            )
            bundle = root / "bundle"
            bundle.mkdir()
            outside = root / "outside.sql"
            outside.write_text("select 1\n", encoding="utf-8")
            manifest = {
                "downstreamPatches": [
                    {
                        "repository": "warehouse",
                        "sourcePath": "models/orders.sql",
                        "proposedPath": "../outside.sql",
                    }
                ],
                "contractPatches": [],
            }
            (bundle / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "escapes its directory"):
                validate_bundle(bundle, (project,), dbt_executable="missing")


if __name__ == "__main__":
    unittest.main()
