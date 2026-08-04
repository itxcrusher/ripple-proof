import json
import re
import tomllib
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION = REPO_ROOT / ".github/actions/audit-campaign/action.yml"
AUDIT_WORKFLOW = REPO_ROOT / ".github/workflows/campaign-audit.yml"
TESTS_WORKFLOW = REPO_ROOT / ".github/workflows/tests.yml"
CANONICAL_BUNDLE = REPO_ROOT / "examples/generated-ripple-proof-resolved"
COLLISION_SCRIPT = REPO_ROOT / "scripts/ci_postgres_collision.py"
HYGIENE_SCRIPT = REPO_ROOT / "scripts/check_source_hygiene.py"
RUNTIME_REQUIREMENTS = REPO_ROOT / "requirements.txt"
VALIDATION_REQUIREMENTS = REPO_ROOT / "requirements-validation.txt"
AUDIT_REQUIREMENTS = REPO_ROOT / "requirements-audit.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"


def requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def exact_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in requirement_lines(path):
        if line.count("==") != 1:
            raise AssertionError(f"{path.name} contains a non-exact requirement: {line}")
        package, version = line.split("==", 1)
        if not package or not version:
            raise AssertionError(f"{path.name} contains an invalid exact pin: {line}")
        pins[package.lower()] = version
    return pins


class CampaignAuditSurfaceTests(unittest.TestCase):
    def test_composite_action_is_dependency_and_credential_free(self):
        action = ACTION.read_text(encoding="utf-8")

        self.assertIn("using: composite", action)
        self.assertIn("python -S -m lineage_agent.audit", action)
        self.assertIn("github.action_path", action)
        self.assertNotIn("pip install", action)
        self.assertNotIn("secrets.", action)

    def test_pull_request_workflow_audits_the_canonical_campaign(self):
        workflow = AUDIT_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertIn("uses: ./.github/actions/audit-campaign", workflow)
        self.assertIn("bundle: examples/generated-ripple-proof-resolved", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("clean-clone-demo:", workflow)
        self.assertIn("python -m pip install --no-deps .", workflow)
        self.assertIn("ripple-proof demo", workflow)
        self.assertIn('test "$elapsed" -le 60', workflow)


class TestsWorkflowSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.workflow = TESTS_WORKFLOW.read_text(encoding="utf-8")
        self.manifest = json.loads(
            (CANONICAL_BUNDLE / "manifest.json").read_text(encoding="utf-8")
        )

    def test_workflow_keeps_external_permissions_read_only(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertNotIn("secrets.", self.workflow)
        self.assertNotIn("DATAHUB_GMS", self.workflow)
        self.assertNotIn("GITHUB_TOKEN", self.workflow)
        self.assertNotRegex(self.workflow, re.compile(r"(?m)^\s*gh\s"))

    def test_canonical_four_root_dbt_validation_covers_every_target_repository(self):
        self.assertIn(
            "Validate the canonical four-root campaign with dbt and DuckDB",
            self.workflow,
        )
        self.assertIn(
            "--bundle examples/generated-ripple-proof-resolved",
            self.workflow,
        )

        target_repositories = self.manifest["targetRepositories"]
        self.assertEqual(len(target_repositories), 5)
        self.assertEqual(len(self.manifest["dbtValidation"]["projects"]), 4)
        for repository in target_repositories:
            self.assertEqual(
                self.workflow.count(f"--target-repo examples/{repository}"),
                1,
                repository,
            )

    def test_postgres_job_runs_fresh_rename_rollback_and_collision_refusal(self):
        self.assertIn("postgres-integration:", self.workflow)
        self.assertIn("docker pull postgres:16", self.workflow)
        self.assertIn(
            "python -m lineage_agent.postgres_validation",
            self.workflow,
        )
        self.assertIn(
            "--bundle examples/generated-ripple-proof-resolved",
            self.workflow,
        )
        self.assertIn(
            "python scripts/ci_postgres_collision.py",
            self.workflow,
        )

        collision = COLLISION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("validate_postgres_migration(", collision)
        self.assertIn("source_columns=fixture_columns", collision)
        self.assertIn('"already exists"', collision)
        self.assertIn('cleanup.status == "passed"', collision)

    def test_proof_tampering_regressions_run_explicitly_from_source(self):
        self.assertIn("Run proof-tampering regressions from source", self.workflow)
        self.assertIn("PYTHONPATH: src", self.workflow)
        for pattern in (
            "test_audit.py",
            "test_replay.py",
            "test_postgres_validation.py",
        ):
            self.assertIn(f'-p "{pattern}"', self.workflow)

    def test_compile_and_source_hygiene_are_blocking_gates(self):
        self.assertIn(
            "python -m compileall -q -f src tests scripts",
            self.workflow,
        )
        self.assertIn(
            "python scripts/check_source_hygiene.py",
            self.workflow,
        )
        hygiene = HYGIENE_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("trailing whitespace", hygiene)
        self.assertIn("missing final newline", hygiene)
        self.assertIn("invalid UTF-8", hygiene)

    def test_clean_install_smokes_the_pinned_datahub_mcp_server(self):
        self.assertIn("python -m pip install -r requirements.txt", self.workflow)
        self.assertIn("Smoke-test the pinned DataHub MCP server", self.workflow)
        self.assertIn(
            "from mcp_server_datahub.mcp_server import mcp",
            self.workflow,
        )
        self.assertIn("isinstance(mcp, FastMCP)", self.workflow)
        for package in (
            "fastapi",
            "fastmcp",
            "python-multipart",
            "sse-starlette",
            "starlette",
        ):
            self.assertIn(f'"{package}"', self.workflow)

    def test_dependency_audit_covers_runtime_and_validation_trees(self):
        self.assertIn("dependency-audit:", self.workflow)
        self.assertIn(
            "python -m pip install -r requirements-audit.txt",
            self.workflow,
        )
        self.assertIn("python -m pip_audit", self.workflow)
        self.assertIn("-r requirements.txt", self.workflow)
        self.assertIn("-r requirements-validation.txt", self.workflow)
        self.assertEqual(exact_pins(AUDIT_REQUIREMENTS), {"pip-audit": "2.10.1"})

    def test_dependency_audit_suppresses_only_the_documented_advisory(self):
        # PYSEC-2026-3447 (setuptools) is fixed in 83.0.0, but every published
        # acryl-datahub release caps setuptools<82.0.0, so the fix is
        # unreachable. That is the ONLY advisory the audit may suppress; any
        # new ignore must be justified here deliberately.
        ignored = re.findall(r"--ignore-vuln\s+(\S+)", self.workflow)
        self.assertEqual(ignored, ["PYSEC-2026-3447"])


class DependencyPinSurfaceTests(unittest.TestCase):
    def test_direct_dependency_surfaces_use_exact_pins(self):
        runtime = exact_pins(RUNTIME_REQUIREMENTS)
        validation = exact_pins(VALIDATION_REQUIREMENTS)

        self.assertGreater(len(runtime), 0)
        self.assertGreater(len(validation), 0)

        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        optional = project["project"]["optional-dependencies"]
        self.assertEqual(set(optional["live"]), set(requirement_lines(RUNTIME_REQUIREMENTS)))
        self.assertEqual(
            set(optional["validation"]),
            set(requirement_lines(VALIDATION_REQUIREMENTS)),
        )

    def test_live_http_surface_uses_supported_patched_versions(self):
        runtime = exact_pins(RUNTIME_REQUIREMENTS)

        self.assertEqual(runtime["fastmcp"], "3.4.0")
        self.assertEqual(runtime["fastapi"], "0.141.1")
        self.assertEqual(runtime["starlette"], "1.3.1")
        self.assertEqual(runtime["python-multipart"], "0.0.32")
        self.assertEqual(runtime["sse-starlette"], "3.4.6")
        # Every Starlette 0.x release carries the published multipart,
        # FileResponse range, or host-header advisories.
        self.assertTrue(runtime["starlette"].startswith("1."))
        # fastmcp 3.4.5 exposes no importable `fastmcp.FastMCP`, which breaks
        # mcp-server-datahub at import time.
        self.assertNotEqual(runtime["fastmcp"], "3.4.5")


if __name__ == "__main__":
    unittest.main()
