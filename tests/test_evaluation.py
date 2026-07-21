import json
import tempfile
import unittest
from pathlib import Path

from lineage_agent.evaluation import (
    render_report_html,
    run_evaluation,
    write_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORPUS = PROJECT_ROOT / "evaluation/corpus.json"
CHECKED_REPORT = PROJECT_ROOT / "examples/evaluation/report.json"


class EvaluationTests(unittest.TestCase):
    def test_adversarial_corpus_passes_with_zero_false_repairs(self):
        report = run_evaluation(CORPUS)

        self.assertTrue(report.passed)
        self.assertEqual(
            report.metrics,
            {
                "totalCases": 14,
                "automationExpected": 6,
                "manualReviewExpected": 8,
                "passedCases": 14,
                "casePassRate": 1.0,
                "repairCoverage": 1.0,
                "manualReviewRecall": 1.0,
                "falseRepairs": 0,
                "falseRepairRate": 0.0,
            },
        )
        self.assertTrue(
            all(
                next(
                    check
                    for check in result.checks
                    if check.name == "source repositories unchanged"
                ).passed
                for result in report.results
            )
        )
        self.assertFalse(
            any(
                "\\" in reason
                for result in report.results
                for reason in result.unresolved_reasons
            )
        )

    def test_checked_report_exactly_matches_a_fresh_run(self):
        report = run_evaluation(CORPUS)
        checked = json.loads(CHECKED_REPORT.read_text(encoding="utf-8"))

        self.assertEqual(checked, report.to_dict())

    def test_false_repair_metric_exposes_an_unsafe_expectation_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
            corpus["cases"] = [corpus["cases"][0]]
            corpus["cases"][0]["expectedDecision"] = "manual-review"
            path = root / "corpus.json"
            path.write_text(json.dumps(corpus), encoding="utf-8")

            report = run_evaluation(path)

        self.assertFalse(report.passed)
        self.assertEqual(report.metrics["falseRepairs"], 1)
        self.assertEqual(report.metrics["falseRepairRate"], 1.0)
        self.assertEqual(report.metrics["casePassRate"], 0.0)

    def test_rejects_model_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
            corpus["cases"] = [corpus["cases"][0]]
            corpus["cases"][0]["repositories"][0]["models"][0]["path"] = (
                "../../outside.sql"
            )
            path = root / "corpus.json"
            path.write_text(json.dumps(corpus), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "escapes its repository"):
                run_evaluation(path)

    def test_report_output_requires_explicit_overwrite(self):
        report = run_evaluation(CORPUS)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path, html_path = write_report(report, root)

            self.assertTrue(json_path.is_file())
            self.assertTrue(html_path.is_file())
            self.assertIn("Adversarial repair evaluation", render_report_html(report))
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                write_report(report, root)


if __name__ == "__main__":
    unittest.main()
