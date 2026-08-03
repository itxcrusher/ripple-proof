import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PublicSurfaceTruthTests(unittest.TestCase):
    def test_readme_matches_current_operator_boundaries(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("ripple-proof doctor", readme)
        self.assertIn("strict deterministic parser by default", readme)
        self.assertIn("explicitly supplies `--generator llm`", readme)
        self.assertIn("integrity and internal consistency", readme)
        self.assertIn("does not establish", readme)
        self.assertIn("Fresh verification is a separate live step", readme)
        self.assertIn("Retry the exact same approved publish command", readme)
        self.assertIn("Retry the exact same approved command", readme)
        self.assertIn("five repository candidates", readme)
        self.assertIn("all four selected dbt validation roots", readme)
        self.assertIn("refusal when the destination column already exists", readme)
        self.assertIn("vulnerability auditing", readme)
        self.assertNotIn("default `auto`", readme)
        self.assertNotIn("--write-back", readme)
        self.assertNotIn("complete captured proof chain", readme)

    def test_env_example_cannot_implicitly_enable_llm(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("Planning always defaults to the strict", example)
        self.assertIn("only when you explicitly run RippleProof with --generator llm", example)
        self.assertNotIn("--generator auto", example)


if __name__ == "__main__":
    unittest.main()
