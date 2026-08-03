import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lineage_agent.dbt_validation import (
    _artifact_proof,
    _local_dependencies,
    _profiles,
    _redaction_values,
    _run_step,
    _validation_environment,
    validate_bundle,
    validate_dbt_campaign,
)


class DbtValidationTests(unittest.TestCase):
    def _write_artifacts(
        self,
        target: Path,
        *,
        results: list[dict],
        include_expected: bool = True,
    ):
        target.mkdir(parents=True)
        nodes = {
            "model.warehouse.orders": {
                "unique_id": "model.warehouse.orders",
                "resource_type": "model",
                "package_name": "warehouse",
                "original_file_path": "models/orders.sql",
            }
        }
        if not include_expected:
            nodes = {
                "model.warehouse.unrelated": {
                    "unique_id": "model.warehouse.unrelated",
                    "resource_type": "model",
                    "package_name": "warehouse",
                    "original_file_path": "models/unrelated.sql",
                }
            }
        (target / "manifest.json").write_text(
            json.dumps({"nodes": nodes}),
            encoding="utf-8",
        )
        (target / "run_results.json").write_text(
            json.dumps({"results": results}),
            encoding="utf-8",
        )

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

    def test_successful_zero_node_run_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            self._write_artifacts(target, results=[])

            step, expected, validated = _artifact_proof(
                target,
                (("warehouse", "models/orders.sql"),),
                work_root=root,
            )

            self.assertEqual(step.status, "failed")
            self.assertIn("executed zero nodes", step.output_tail)
            self.assertEqual(expected, ("model.warehouse.orders",))
            self.assertEqual(validated, ())

    def test_unrelated_success_cannot_substitute_for_expected_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            self._write_artifacts(
                target,
                results=[
                    {
                        "unique_id": "model.warehouse.unrelated",
                        "status": "success",
                    }
                ],
            )

            step, expected, validated = _artifact_proof(
                target,
                (("warehouse", "models/orders.sql"),),
                work_root=root,
            )

            self.assertEqual(step.status, "failed")
            self.assertIn("was not executed", step.output_tail)
            self.assertEqual(expected, ("model.warehouse.orders",))
            self.assertEqual(validated, ())

    def test_unrelated_manifest_node_cannot_match_generated_source_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            self._write_artifacts(
                target,
                include_expected=False,
                results=[
                    {
                        "unique_id": "model.warehouse.unrelated",
                        "status": "success",
                    }
                ],
            )

            step, expected, validated = _artifact_proof(
                target,
                (("warehouse", "models/orders.sql"),),
                work_root=root,
            )

            self.assertEqual(step.status, "failed")
            self.assertIn("manifest contained 0", step.output_tail)
            self.assertEqual(expected, ())
            self.assertEqual(validated, ())

    def test_exact_expected_model_is_bound_into_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            self._write_artifacts(
                target,
                results=[
                    {
                        "unique_id": "model.warehouse.orders",
                        "status": "success",
                    }
                ],
            )

            step, expected, validated = _artifact_proof(
                target,
                (("warehouse", "models/orders.sql"),),
                work_root=root,
            )

            self.assertEqual(step.status, "passed")
            self.assertEqual(expected, ("model.warehouse.orders",))
            self.assertEqual(validated, expected)
            self.assertIn("model.warehouse.orders", step.output_tail)

    def test_no_generated_model_in_validation_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            self._write_artifacts(
                target,
                results=[
                    {
                        "unique_id": "model.warehouse.orders",
                        "status": "success",
                    }
                ],
            )

            step, expected, validated = _artifact_proof(
                target,
                (),
                work_root=root,
            )

            self.assertEqual(step.status, "failed")
            self.assertIn("No generated dbt model", step.output_tail)
            self.assertEqual(expected, ())
            self.assertEqual(validated, ())

    def test_subprocess_uses_allowlisted_environment_and_redacts_output(self):
        source_environment = {
            "PATH": "C:/tools",
            "TEMP": "C:/temp",
            "OPENAI_API_KEY": "environment-secret-123",
            "AWS_SECRET_ACCESS_KEY": "aws-secret-value-456",
            "HTTP_PROXY": "http://proxy.example",
        }
        environment = _validation_environment(source_environment)
        secrets = _redaction_values(source_environment)
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                "api_key=inline-secret-789 "
                "Authorization: Bearer abcdefghijklmnop "
                "postgresql://user:database-password@db.example/orders "
                "environment-secret-123"
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch(
            "lineage_agent.dbt_validation.subprocess.run",
            return_value=completed,
        ) as run:
            step = _run_step(
                "dbt-build",
                ["dbt", "build"],
                work_root=Path(temp_dir),
                environment=environment,
                timeout_seconds=10,
                sensitive_values=secrets,
            )

        passed_environment = run.call_args.kwargs["env"]
        self.assertEqual(passed_environment["PATH"], "C:/tools")
        self.assertNotIn("OPENAI_API_KEY", passed_environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", passed_environment)
        self.assertNotIn("HTTP_PROXY", passed_environment)
        for secret in (
            "inline-secret-789",
            "abcdefghijklmnop",
            "database-password",
            "environment-secret-123",
        ):
            self.assertNotIn(secret, step.output_tail)
        self.assertIn("<redacted>", step.output_tail)

    def test_network_reliant_package_is_rejected_before_dbt_runs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "warehouse"
            root.mkdir()
            (root / "dbt_project.yml").write_text(
                "name: warehouse\nprofile: warehouse\n",
                encoding="utf-8",
            )
            (root / "packages.yml").write_text(
                "packages:\n  - package: dbt-labs/dbt_utils\n    version: 1.3.0\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Network-reliant dbt packages"):
                _local_dependencies({"warehouse": root})

    def test_network_reliant_dbt_mesh_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "warehouse"
            root.mkdir()
            (root / "dbt_project.yml").write_text(
                "name: warehouse\nprofile: warehouse\n",
                encoding="utf-8",
            )
            (root / "dependencies.yml").write_text(
                "projects:\n  - name: upstream_project\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Network-reliant dbt projects"):
                _local_dependencies({"warehouse": root})

    def test_duckdb_profile_disables_external_access_and_extension_downloads(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            project = root / "warehouse"
            project.mkdir()
            (project / "dbt_project.yml").write_text(
                "name: warehouse\nprofile: warehouse\n",
                encoding="utf-8",
            )

            profiles = _profiles({"warehouse": project}, root / "validation.duckdb")

            self.assertIn("enable_external_access: false", profiles)
            self.assertIn("autoinstall_known_extensions: false", profiles)
            self.assertIn("autoload_known_extensions: false", profiles)


if __name__ == "__main__":
    unittest.main()
