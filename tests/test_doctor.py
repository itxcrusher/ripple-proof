import os
import subprocess
import unittest
from unittest.mock import patch

from lineage_agent.doctor import (
    ReadinessCheck,
    _mcp_environment,
    check_configuration,
    check_docker,
    check_local_datahub,
    render_results,
)


def completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class DoctorTests(unittest.TestCase):
    @patch("lineage_agent.doctor.load_dotenv")
    def test_configuration_never_prints_token(self, load_dotenv):
        secret = "do-not-print-this-token"
        with patch.dict(
            os.environ,
            {
                "DATAHUB_GMS_URL": "http://localhost:8080",
                "DATAHUB_GMS_TOKEN": secret,
            },
            clear=False,
        ):
            result = check_configuration()

        self.assertEqual(result.status, "passed")
        self.assertNotIn(secret, result.detail)
        load_dotenv.assert_called()

    @patch("lineage_agent.doctor.shutil.which", return_value="docker")
    def test_docker_engine_failure_is_actionable(self, which):
        result = check_docker(
            lambda *args, **kwargs: completed(1, stderr="engine is not running")
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("engine is not running", result.detail)

    @patch("lineage_agent.doctor.shutil.which", return_value="datahub")
    def test_silent_datahub_failure_has_recovery_commands(self, which):
        result = check_local_datahub(
            "http://localhost:8080",
            lambda *args, **kwargs: completed(1),
        )

        self.assertEqual(result.status, "failed")
        self.assertIn("datahub docker check", result.detail)
        self.assertIn("datahub docker quickstart", result.detail)

    def test_remote_endpoint_skips_local_quickstart(self):
        result = check_local_datahub("https://datahub.example.com")

        self.assertEqual(result.status, "skipped")

    def test_mcp_subprocess_environment_drops_unrelated_secrets(self):
        with patch.dict(
            os.environ,
            {
                "DATAHUB_GMS_TOKEN": "datahub-token",
                "OPENAI_API_KEY": "openai-secret",
                "ELEVENLABS_API_KEY": "voice-secret",
            },
            clear=False,
        ):
            environment = _mcp_environment("http://localhost:8080")

        self.assertEqual(environment["DATAHUB_GMS_TOKEN"], "datahub-token")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("ELEVENLABS_API_KEY", environment)

    def test_renderer_separates_pass_skip_and_failure(self):
        output = render_results(
            [
                ReadinessCheck("One", "passed", "ready"),
                ReadinessCheck("Two", "skipped", "not local"),
                ReadinessCheck("Three", "failed", "broken"),
            ]
        )

        self.assertIn("LIVE READINESS: FAILED", output)
        self.assertIn("[PASSED] One", output)
        self.assertIn("[SKIPPED] Two", output)
        self.assertIn("[FAILED] Three", output)


if __name__ == "__main__":
    unittest.main()
