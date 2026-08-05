import json
import shutil
import tempfile
import unittest
from pathlib import Path

from lineage_agent.audit import (
    _lineage_check,
    audit_campaign,
    render_audit_text,
    write_audit,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLETE_BUNDLE = REPO_ROOT / "examples" / "generated-ripple-proof-resolved"


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

    def test_missing_publication_with_a_decision_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            (copied / "publication.json").unlink()

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "failed")
            self.assertEqual(audit.checks[0].check_id, "bundle-load")
            self.assertIn("without publication proof", audit.checks[0].detail)

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
            self.assertEqual(audit.checks[0].check_id, "bundle-load")
            self.assertIn("invalid repository entry", audit.checks[0].detail)

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

    def test_partial_fabricated_publication_cannot_pass_the_campaign_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            publication_path = copied / "publication.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            only = publication["repositories"][0]
            only["repository"] = "example.invalid/one"
            only["pullRequest"]["url"] = "https://github.com/example.invalid/one/pull/3"
            publication["repositories"] = [only]
            publication_path.write_text(json.dumps(publication), encoding="utf-8")

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "failed")
            self.assertEqual(audit.checks[0].check_id, "bundle-load")
            self.assertIn("Publication proof", audit.checks[0].detail)

    def test_publication_requires_exact_file_scope_commit_and_check_conclusion(self):
        mutations = (
            (
                "file scope",
                lambda item: item.__setitem__(
                    "files", [{"path": "models/wrong.sql", "sha256": "a" * 64}]
                ),
            ),
            ("commit SHA", lambda item: item.__setitem__("commitSha", "not-a-sha")),
            (
                "required check conclusions",
                lambda item: item["checks"]["checks"][0].__setitem__(
                    "state", "FAILURE"
                ),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp_dir:
                copied = Path(temp_dir) / "campaign"
                shutil.copytree(COMPLETE_BUNDLE, copied)
                publication_path = copied / "publication.json"
                publication = json.loads(publication_path.read_text(encoding="utf-8"))
                mutate(publication["repositories"][0])
                publication_path.write_text(json.dumps(publication), encoding="utf-8")

                audit = audit_campaign(copied)

                self.assertEqual(audit.status, "failed")
                self.assertIn(expected, audit.checks[0].detail)

    def test_self_consistent_partial_decision_cannot_pass_the_campaign_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            copied = Path(temp_dir) / "campaign"
            shutil.copytree(COMPLETE_BUNDLE, copied)
            decision_path = copied / "decision.json"
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            decision["assets"] = [decision["assets"][0]]
            decision["verifiedAssets"] = list(decision["assets"])
            decision_path.write_text(json.dumps(decision), encoding="utf-8")

            audit = audit_campaign(copied)

            self.assertEqual(audit.status, "failed")
            self.assertIn("complete covered asset set", audit.checks[0].detail)

    def test_lineage_accounting_accepts_coalesced_patch_asset_arrays(self):
        manifest = {
            "lineageAssets": [{"urn": "urn:a"}, {"urn": "urn:b"}],
            "downstreamPatches": [
                {
                    "assetUrns": ["urn:a", "urn:b"],
                    "assetNames": ["a", "b"],
                }
            ],
            "protectedByContract": [],
            "manualReview": [],
        }

        check = _lineage_check(manifest)

        self.assertEqual(check.status, "passed")
        self.assertIn("All 2 reachable asset", check.detail)

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
