import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from lineage_agent import cli


class CliTests(unittest.TestCase):
    def test_help_exposes_the_complete_operator_path(self):
        output = io.StringIO()
        with redirect_stdout(output):
            cli.main(["--help"])

        rendered = output.getvalue()
        self.assertIn("ripple-proof demo", rendered)
        self.assertIn("plan", rendered)
        self.assertIn("audit", rendered)
        self.assertIn("publish", rendered)
        self.assertIn("record-decision", rendered)

    def test_unknown_command_fails_with_recovery_guidance(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            cli.main(["unknown"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Unknown command", output.getvalue())
        self.assertIn("ripple-proof <command>", output.getvalue())

    @patch("lineage_agent.cli.webbrowser.open")
    @patch("lineage_agent.audit.audit_campaign")
    @patch("lineage_agent.audit.render_audit_text", return_value="CAMPAIGN AUDIT: PASSED")
    def test_demo_audits_and_can_open_a_review(
        self,
        render_audit,
        audit_campaign,
        open_browser,
    ):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "manifest.json").write_text("{}", encoding="utf-8")
            review = bundle / "campaign.html"
            review.write_text("<html></html>", encoding="utf-8")
            audit_campaign.return_value.status = "passed"
            output = io.StringIO()

            with redirect_stdout(output):
                cli.main(["demo", "--bundle", str(bundle), "--open"])

        audit_campaign.assert_called_once_with(bundle.resolve())
        render_audit.assert_called_once_with(audit_campaign.return_value)
        open_browser.assert_called_once_with(review.resolve().as_uri())
        self.assertIn("CAMPAIGN AUDIT: PASSED", output.getvalue())
        self.assertIn("Review page:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
