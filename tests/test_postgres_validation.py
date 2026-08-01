import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lineage_agent.postgres_validation import (
    validate_bundle,
    validate_postgres_evidence,
    validate_postgres_migration,
    write_postgres_evidence,
)


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
MIGRATION = 'ALTER TABLE "analytics"."orders" RENAME COLUMN "user_id" TO "customer_id";'


def fake_docker(command, **kwargs):
    if command[1] == "run":
        return subprocess.CompletedProcess(command, 0, "container-id\n", "")
    if command[1] == "logs":
        return subprocess.CompletedProcess(
            command,
            0,
            "PostgreSQL init process complete; ready for start up.\n",
            "",
        )
    if "pg_isready" in command:
        return subprocess.CompletedProcess(command, 0, "accepting connections\n", "")
    if "psql" in command:
        sql = kwargs.get("input") or ""
        if "CREATE TABLE" in sql:
            return subprocess.CompletedProcess(
                command,
                0,
                "version:16.10\nbefore:user_id\n",
                "",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            "after:customer_id\nrollback:user_id\n",
            "",
        )
    if command[1:3] == ["rm", "--force"]:
        return subprocess.CompletedProcess(command, 0, "container-id\n", "")
    raise AssertionError(f"Unexpected Docker command: {command}")


def write_bundle(root: Path) -> Path:
    bundle = root / "bundle"
    bundle.mkdir()
    (bundle / "migration.sql").write_text(f"{MIGRATION}\n", encoding="utf-8")
    manifest = {
        "source": {
            "urn": SOURCE_URN,
            "dataset": "analytics.orders",
            "rename": {"oldColumn": "user_id", "newColumn": "customer_id"},
        },
        "migration": "migration.sql",
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    return bundle


class PostgresValidationTests(unittest.TestCase):
    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run", side_effect=fake_docker)
    def test_proves_rename_and_rollback_in_networkless_container(self, run, _which):
        result = validate_postgres_migration(
            source_urn=SOURCE_URN,
            old_column="user_id",
            new_column="customer_id",
            migration_sql=MIGRATION,
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.engine_version, "16.10")
        self.assertEqual(result.before_columns, ("user_id",))
        self.assertEqual(result.after_columns, ("customer_id",))
        self.assertEqual(result.rollback_columns, ("user_id",))
        self.assertEqual(
            [step.name for step in result.steps],
            [
                "container-start",
                "postgres-ready",
                "fixture-setup",
                "migration-transaction",
                "container-cleanup",
            ],
        )
        start = result.steps[0].command
        self.assertIn("--network", start)
        self.assertIn("none", start)
        self.assertIn("--pull", start)
        self.assertIn("never", start)
        self.assertNotIn("POSTGRES_PASSWORD", " ".join(start))
        self.assertTrue(any(call.args[0][1:3] == ["rm", "--force"] for call in run.call_args_list))

    @patch("lineage_agent.postgres_validation.time.sleep")
    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run")
    def test_waits_for_completed_initialization_before_accepting_readiness(
        self, run, _which, _sleep
    ):
        log_reads = 0

        def delayed_initialization(command, **kwargs):
            nonlocal log_reads
            if command[1] == "logs":
                log_reads += 1
                output = (
                    "PostgreSQL init process complete; ready for start up.\n"
                    if log_reads == 2
                    else "database system is ready to accept connections\n"
                )
                return subprocess.CompletedProcess(command, 0, output, "")
            return fake_docker(command, **kwargs)

        run.side_effect = delayed_initialization

        result = validate_postgres_migration(
            source_urn=SOURCE_URN,
            old_column="user_id",
            new_column="customer_id",
            migration_sql=MIGRATION,
        )

        self.assertEqual(result.status, "passed")
        self.assertEqual(log_reads, 2)
        calls = [call.args[0] for call in run.call_args_list]
        ready_index = next(index for index, command in enumerate(calls) if "pg_isready" in command)
        second_log_index = [
            index for index, command in enumerate(calls) if command[1] == "logs"
        ][1]
        self.assertGreater(ready_index, second_log_index)

    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run")
    def test_rejects_any_sql_beyond_the_constrained_rename(self, run, _which):
        with self.assertRaisesRegex(ValueError, "exact constrained rename"):
            validate_postgres_migration(
                source_urn=SOURCE_URN,
                old_column="user_id",
                new_column="customer_id",
                migration_sql=f"{MIGRATION} DROP TABLE analytics.orders;",
            )

        run.assert_not_called()

    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run")
    def test_rejects_non_postgres_container_images(self, run, _which):
        with self.assertRaisesRegex(ValueError, "official postgres image"):
            validate_postgres_migration(
                source_urn=SOURCE_URN,
                old_column="user_id",
                new_column="customer_id",
                migration_sql=MIGRATION,
                image="example.com/untrusted:latest",
            )

        run.assert_not_called()

    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run", side_effect=fake_docker)
    def test_writes_manifest_bound_evidence_for_an_existing_bundle(self, _run, _which):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            result = validate_bundle(bundle)
            path = write_postgres_evidence(bundle, result)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            manifest_sha = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()

            validated = validate_postgres_evidence(
                evidence,
                campaign_manifest_sha256=manifest_sha,
                migration_sql=f"{MIGRATION}\n",
                source_dataset="analytics.orders",
                old_column="user_id",
                new_column="customer_id",
            )

            self.assertEqual(validated["validation"]["status"], "passed")
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_postgres_evidence(bundle, result)

    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run", side_effect=fake_docker)
    def test_evidence_rejects_a_changed_migration(self, _run, _which):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            result = validate_bundle(bundle)
            path = write_postgres_evidence(bundle, result)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            manifest_sha = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()

            with self.assertRaisesRegex(ValueError, "campaign migration"):
                validate_postgres_evidence(
                    evidence,
                    campaign_manifest_sha256=manifest_sha,
                    migration_sql="SELECT 1;\n",
                    source_dataset="analytics.orders",
                    old_column="user_id",
                    new_column="customer_id",
                )

    @patch("lineage_agent.postgres_validation.shutil.which", return_value="docker")
    @patch("lineage_agent.postgres_validation.subprocess.run", side_effect=fake_docker)
    def test_evidence_rejects_a_passing_step_with_nonzero_exit(self, _run, _which):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir))
            result = validate_bundle(bundle)
            path = write_postgres_evidence(bundle, result)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            evidence["validation"]["steps"][0]["exitCode"] = 1
            manifest_sha = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()

            with self.assertRaisesRegex(ValueError, "invalid step"):
                validate_postgres_evidence(
                    evidence,
                    campaign_manifest_sha256=manifest_sha,
                    migration_sql=f"{MIGRATION}\n",
                    source_dataset="analytics.orders",
                    old_column="user_id",
                    new_column="customer_id",
                )


if __name__ == "__main__":
    unittest.main()
