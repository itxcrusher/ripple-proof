import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lineage_agent.audit import audit_campaign, render_audit_text, write_audit


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLETE_BUNDLE = REPO_ROOT / "examples" / "generated-rich-propagate-resolved"


class AuditTests(unittest.TestCase):
    def test_checked_campaign_passes_every_offline_audit(self):
        audit = audit_campaign(COMPLETE_BUNDLE)

        self.assertEqual(audit.status, "passed")
        self.assertEqual(len(audit.checks), 11)
        self.assertTrue(all(check.status == "passed" for check in audit.checks))
        self.assertEqual(audit.metrics["generatedFiles"], 8)
        self.assertEqual(audit.metrics["publishedPullRequests"], 4)
        self.assertIn("offline verification of captured evidence", audit.summary)
        rendered = render_audit_text(audit)
        self.assertIn("CAMPAIGN AUDIT: PASSED", rendered)
        self.assertIn("[PASS] DataHub MCP capture", rendered)

    def test_missing_publication_is_reported_as_incomplete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            (copied / "publication.json").unlink()

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "incomplete")
            delivery = next(check for check in audit.checks if check.check_id == "github-delivery")
            self.assertEqual(delivery.status, "incomplete")

    def test_tampered_proposed_file_fails_hash_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            manifest = json.loads((copied / "manifest.json").read_text(encoding="utf-8"))
            proposed = copied / manifest["downstreamPatches"][0]["proposedPath"]
            proposed.write_text(proposed.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "failed")
            integrity = next(check for check in audit.checks if check.check_id == "artifact-integrity")
            self.assertEqual(integrity.status, "failed")
            self.assertIn("SHA-256 mismatch", integrity.detail)

    def test_malformed_publication_entry_fails_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            publication_path = copied / "publication.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["repositories"] = ["not-an-object"]
            publication_path.write_text(json.dumps(publication), encoding="utf-8")

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "failed")
            delivery = next(check for check in audit.checks if check.check_id == "github-delivery")
            self.assertEqual(delivery.status, "failed")
            self.assertIn("invalid repository entry", delivery.detail)

    def test_invalid_manifest_bound_sidecar_fails_bundle_load(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            publication_path = copied / "publication.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["campaignManifestSha256"] = "0" * 64
            publication_path.write_text(json.dumps(publication), encoding="utf-8")

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "failed")
            self.assertEqual(audit.checks[0].check_id, "bundle-load")
            self.assertIn("does not match", audit.checks[0].detail)

    def test_recorded_audit_is_manifest_bound_and_requires_force(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            existing = copied / "verification.json"
            if existing.exists():
                existing.unlink()
            audit = audit_campaign(copied)

            path = write_audit(audit, copied)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(
                payload["campaignManifestSha256"],
                audit.campaign_manifest_sha256,
            )
            with self.assertRaisesRegex(ValueError, "already exists"):
                write_audit(audit, copied)
            write_audit(audit, copied, overwrite=True)


if __name__ == "__main__":
    unittest.main()
