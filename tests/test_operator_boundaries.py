import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from lineage_agent import context_regression, demo_setup
from lineage_agent.main import _arguments as plan_arguments
from lineage_agent.main import main as plan_main
from lineage_agent.mutation_safety import (
    MutationSafetyError,
    NON_LOOPBACK_APPROVAL_TOKEN,
    is_loopback_datahub_url,
    require_datahub_mutation_permission,
)


REMOTE_DATAHUB_URL = "https://datahub.example.com"


class GeneratorBoundaryTests(unittest.TestCase):
    def test_plan_defaults_to_the_deterministic_parser(self):
        with patch.dict(
            os.environ, {"OPENAI_API_KEY": "ambient-secret"}, clear=False
        ):
            args = plan_arguments([])

        self.assertEqual(args.generator, "deterministic")

    def test_plan_parser_has_no_auto_generator_mode(self):
        output = io.StringIO()

        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            plan_arguments(["--generator", "auto"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", output.getvalue())

    def test_legacy_writeback_is_refused_before_planning_opens(self):
        output = io.StringIO()

        with (
            patch("lineage_agent.main.asyncio.run") as run_loop,
            redirect_stderr(output),
            self.assertRaises(SystemExit) as raised,
        ):
            plan_main(["--write-back"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not manifest-bound", output.getvalue())
        self.assertIn("record-decision", output.getvalue())
        run_loop.assert_not_called()


class MutationEndpointTests(unittest.TestCase):
    def test_only_real_loopback_hosts_are_accepted_by_default(self):
        self.assertTrue(is_loopback_datahub_url("http://localhost:8080"))
        self.assertTrue(is_loopback_datahub_url("https://127.0.0.42:8080"))
        self.assertTrue(is_loopback_datahub_url("http://[::1]:8080"))
        self.assertFalse(is_loopback_datahub_url("https://localhost.example.com"))
        self.assertFalse(is_loopback_datahub_url("https://127.0.0.1.example.com"))

    def test_non_loopback_requires_both_override_and_exact_token(self):
        with self.assertRaisesRegex(MutationSafetyError, "Refusing to mutate"):
            require_datahub_mutation_permission(endpoint=REMOTE_DATAHUB_URL)

        with self.assertRaisesRegex(MutationSafetyError, "approval was not exact"):
            require_datahub_mutation_permission(
                endpoint=REMOTE_DATAHUB_URL,
                allow_non_loopback=True,
                approval_token="close-enough",
            )

        self.assertEqual(
            require_datahub_mutation_permission(
                endpoint=REMOTE_DATAHUB_URL,
                allow_non_loopback=True,
                approval_token=NON_LOOPBACK_APPROVAL_TOKEN,
            ),
            REMOTE_DATAHUB_URL,
        )

    def test_malformed_or_credential_bearing_urls_fail_closed(self):
        for endpoint in (
            "datahub.example.com",
            "file:///tmp/datahub",
            "https://user:secret@localhost:8080",
            "http://localhost:not-a-port",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(MutationSafetyError):
                    require_datahub_mutation_permission(endpoint=endpoint)


class MutationCommandTests(unittest.TestCase):
    @patch("lineage_agent.demo_setup.DataHubClient")
    def test_demo_setup_binds_client_to_the_validated_loopback_url(
        self, datahub_client
    ):
        with (
            patch.dict(
                os.environ,
                {
                    "DATAHUB_GMS_URL": "http://127.0.0.1:8080",
                    "DATAHUB_GMS_TOKEN": "test-token",
                },
                clear=False,
            ),
            redirect_stdout(io.StringIO()),
        ):
            demo_setup.main([])

        datahub_client.assert_called_once_with(
            server="http://127.0.0.1:8080",
            token="test-token",
        )

    @patch("lineage_agent.demo_setup.DataHubClient")
    def test_demo_setup_rejects_ambient_remote_url_before_client_open(
        self, datahub_client
    ):
        output = io.StringIO()

        with (
            patch.dict(
                os.environ, {"DATAHUB_GMS_URL": REMOTE_DATAHUB_URL}, clear=False
            ),
            redirect_stderr(output),
            self.assertRaises(SystemExit) as raised,
        ):
            demo_setup.main([])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("non-loopback", output.getvalue())
        datahub_client.assert_not_called()

    @patch("lineage_agent.demo_setup.DataHubClient")
    def test_override_without_exact_token_still_precedes_client_open(
        self, datahub_client
    ):
        with (
            patch.dict(
                os.environ, {"DATAHUB_GMS_URL": REMOTE_DATAHUB_URL}, clear=False
            ),
            redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            demo_setup.main(
                [
                    "--allow-non-loopback-datahub-mutation",
                    "--non-loopback-approval-token",
                    "wrong",
                ]
            )

        datahub_client.assert_not_called()

    def test_exact_demo_override_is_recognized_without_opening_a_client(self):
        with patch.dict(
            os.environ, {"DATAHUB_GMS_URL": REMOTE_DATAHUB_URL}, clear=False
        ):
            args = demo_setup._arguments(
                [
                    "--allow-non-loopback-datahub-mutation",
                    "--non-loopback-approval-token",
                    NON_LOOPBACK_APPROVAL_TOKEN,
                ]
            )

        self.assertEqual(args.datahub_url, REMOTE_DATAHUB_URL)

    def test_context_regression_rejects_remote_url_before_async_work(self):
        output = io.StringIO()

        with (
            patch.dict(
                os.environ, {"DATAHUB_GMS_URL": REMOTE_DATAHUB_URL}, clear=False
            ),
            patch("lineage_agent.context_regression.asyncio.run") as run_loop,
            redirect_stderr(output),
            self.assertRaises(SystemExit) as raised,
        ):
            context_regression.main(
                ["--bundle", "demo", "--confirm-demo-mutation"]
            )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("non-loopback", output.getvalue())
        run_loop.assert_not_called()


class ProgrammaticMutationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_regression_checks_endpoint_before_bundle_or_graph_reads(self):
        with (
            patch.dict(
                os.environ, {"DATAHUB_GMS_URL": REMOTE_DATAHUB_URL}, clear=False
            ),
            patch("lineage_agent.context_regression._load_captured") as load,
            patch(
                "lineage_agent.context_regression._new_mutation_graph"
            ) as graph_factory,
        ):
            with self.assertRaisesRegex(MutationSafetyError, "Refusing to mutate"):
                await context_regression.run_regression(
                    unittest.mock.sentinel.bundle,
                    timeout_seconds=30,
                    record_evidence=False,
                )

        load.assert_not_called()
        graph_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
