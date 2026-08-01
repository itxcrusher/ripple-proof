import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION = REPO_ROOT / ".github/actions/audit-campaign/action.yml"
WORKFLOW = REPO_ROOT / ".github/workflows/campaign-audit.yml"


class CampaignAuditSurfaceTests(unittest.TestCase):
    def test_composite_action_is_dependency_and_credential_free(self):
        action = ACTION.read_text(encoding="utf-8")

        self.assertIn("using: composite", action)
        self.assertIn("python -S -m lineage_agent.audit", action)
        self.assertIn("github.action_path", action)
        self.assertNotIn("pip install", action)
        self.assertNotIn("secrets.", action)

    def test_pull_request_workflow_audits_the_canonical_campaign(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("pull_request:", workflow)
        self.assertIn("uses: ./.github/actions/audit-campaign", workflow)
        self.assertIn("bundle: examples/generated-ripple-proof-resolved", workflow)
        self.assertIn("permissions:\n  contents: read", workflow)
        self.assertIn("clean-clone-demo:", workflow)
        self.assertIn("python -m pip install --no-deps .", workflow)
        self.assertIn("ripple-proof demo", workflow)
        self.assertIn('test "$elapsed" -le 60', workflow)


if __name__ == "__main__":
    unittest.main()
