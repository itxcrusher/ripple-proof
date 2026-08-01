import tempfile
import unittest
from pathlib import Path

from lineage_agent.main import GeneratedChange, ImpactReport
from lineage_agent.reporting import render_markdown_report, write_markdown_report


class MarkdownReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result = GeneratedChange(
            change_sql=(
                'ALTER TABLE "analytics"."orders" '
                'RENAME COLUMN "user_id" TO "customer_id";'
            ),
            impact=ImpactReport(
                downstream=[{"urn": "urn:downstream"}],
                summary="DataHub found one downstream asset.\n\nRemediation plan:\n- Review it.",
            ),
            generator="deterministic parser",
        )

    def test_renders_pr_ready_sections_and_safety_warning(self) -> None:
        report = render_markdown_report(
            self.result,
            source_urn="urn:source",
            instruction="rename user_id to customer_id",
        )

        self.assertIn("# RippleProof Change Impact Report", report)
        self.assertIn("## Proposed SQL", report)
        self.assertIn("```sql", report)
        self.assertIn("## DataHub impact analysis", report)
        self.assertIn("does not execute this SQL", report)
        self.assertIn("Missing lineage", report)

    def test_escapes_inline_metadata_and_flattens_newlines(self) -> None:
        report = render_markdown_report(
            self.result,
            source_urn="urn:`source`",
            instruction="rename user_id\nto customer_id",
        )

        self.assertIn("`urn:&#96;source&#96;`", report)
        self.assertIn("`rename user_id to customer_id`", report)

    def test_includes_generated_artifact_summary_when_provided(self) -> None:
        report = render_markdown_report(
            self.result,
            source_urn="urn:source",
            instruction="rename user_id to customer_id",
            artifact_summary="- `models/orders_daily.sql`",
        )

        self.assertIn("## Generated downstream artifacts", report)
        self.assertIn("`models/orders_daily.sql`", report)

    def test_writes_nested_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "reports" / "impact.md"

            written = write_markdown_report("report\n", destination)

            self.assertEqual(written, destination.resolve())
            self.assertEqual(destination.read_text(encoding="utf-8"), "report\n")


if __name__ == "__main__":
    unittest.main()
