import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from lineage_agent.context_guard import (
    ContextVerification,
    canonical_context_snapshot,
    captured_context_from_manifest,
    context_fingerprint,
    context_manifest_record,
    write_context_verification_evidence,
)
from lineage_agent.replay import load_campaign, render_replay_html, write_replay


def write_bundle(root: Path, mode: str) -> Path:
    bundle = root / mode
    patches = bundle / "patches"
    patches.mkdir(parents=True)
    (bundle / "migration.sql").write_text(
        'ALTER TABLE "analytics"."orders" RENAME COLUMN "user_id" TO "customer_id";\n',
        encoding="utf-8",
    )
    (bundle / "impact-report.md").write_text("# Impact\n", encoding="utf-8")
    (patches / "orders.sql.patch").write_text(
        "--- a/orders.sql\n+++ b/orders.sql\n@@ -1 +1 @@\n-user_id\n+customer_id\n",
        encoding="utf-8",
    )
    manifest = {
        "schemaVersion": 5,
        "source": {
            "urn": "urn:source",
            "dataset": "analytics.orders",
            "platform": "postgres",
            "instruction": "rename user_id to customer_id",
            "rename": {"oldColumn": "user_id", "newColumn": "customer_id"},
            "owners": [
                {
                    "urn": "urn:li:corpGroup:platform-data",
                    "name": "Platform Data",
                    "type": "TECHNICAL_OWNER",
                }
            ],
        },
        "migration": "migration.sql",
        "impactReport": "impact-report.md",
        "downstreamColumnMode": mode,
        "readyForReview": True,
        "targetRepositories": ["warehouse"],
        "lineageAssets": [
            {
                "urn": "urn:orders_daily",
                "name": "orders_daily",
                "type": "DATASET",
                "platform": "dbt",
                "degree": 1,
                "columns": ["user_id"],
                "evidence": "column",
                "owners": [
                    {
                        "urn": "urn:li:corpGroup:analytics",
                        "name": "Analytics Engineering",
                        "type": "TECHNICAL_OWNER",
                    }
                ],
            }
        ],
        "dataHubContext": {
            "capture": "live-mcp",
            "reads": [{"tool": "get_lineage(column)", "detail": "One match."}],
        },
        "dbtValidation": {
            "status": "passed",
            "adapter": "duckdb",
            "dbtVersion": "1.9.10",
            "isolation": "temporary copied repositories",
            "projects": [
                {
                    "repository": "warehouse",
                    "status": "passed",
                    "steps": [
                        {
                            "name": "dbt-build",
                            "command": ["dbt", "build"],
                            "exitCode": 0,
                            "status": "passed",
                            "summary": "PASS=2 WARN=0 ERROR=0 SKIP=0 TOTAL=2",
                            "outputTail": "Completed successfully",
                        }
                    ],
                }
            ],
        },
        "downstreamPatches": [
            {
                "assetUrn": "urn:orders_daily",
                "assetName": "orders_daily",
                "repository": "warehouse",
                "sourcePath": "models/orders.sql",
                "diffPath": "patches/orders.sql.patch",
            }
        ],
        "contractPatches": [],
        "protectedByContract": [],
        "manualReview": [],
        "reviewAssignments": [
            {
                "assetUrn": "urn:source",
                "assetName": "analytics.orders",
                "disposition": "source-change",
                "reason": "Review the source migration.",
                "reviewers": [
                    {
                        "urn": "urn:li:corpGroup:platform-data",
                        "name": "Platform Data",
                        "type": "TECHNICAL_OWNER",
                    }
                ],
                "routingStatus": "assigned",
            },
            {
                "assetUrn": "urn:orders_daily",
                "assetName": "orders_daily",
                "disposition": "patched",
                "reason": "Review the generated patch.",
                "reviewers": [
                    {
                        "urn": "urn:li:corpGroup:analytics",
                        "name": "Analytics Engineering",
                        "type": "TECHNICAL_OWNER",
                    }
                ],
                "routingStatus": "assigned",
            },
        ],
        "modelMappingResolutions": [
            {
                "assetUrn": "urn:orders_daily",
                "assetName": "orders_daily",
                "selectedCandidate": "warehouse/models/orders_daily.sql",
                "candidates": [
                    "warehouse/models/orders_daily.sql",
                    "shadow/models/orders_daily.sql",
                ],
                "unselectedCandidates": ["shadow/models/orders_daily.sql"],
                "resolutionSource": "reviewer-override",
                "resolutionSha256": "a" * 64,
            }
        ],
        "validations": [
            {"id": "patch-integrity", "status": "passed", "detail": "Hashes match."}
        ],
        "safety": {
            "sqlExecuted": False,
            "targetRepositoriesModified": False,
            "humanReviewRequired": True,
            "downstreamOutputContractMayChange": mode == "propagate",
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


class ReplayTests(unittest.TestCase):
    def test_loads_artifacts_and_renders_two_real_modes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            preserve = load_campaign(write_bundle(root, "preserve"))
            propagate = load_campaign(write_bundle(root, "propagate"))

            html = render_replay_html((preserve, propagate))

            self.assertIn("Preserve contract", html)
            self.assertIn("Propagate rename", html)
            self.assertIn("orders_daily", html)
            self.assertIn("customer_id", html)
            self.assertIn("prefers-reduced-motion", html)
            self.assertIn("Executable dbt validation", html)
            self.assertIn("DataHub review decision", html)
            self.assertIn("GitHub delivery", html)
            self.assertIn("Review routing", html)
            self.assertIn("Analytics Engineering", html)
            self.assertIn("Model mapping decisions", html)
            self.assertIn("warehouse/models/orders_daily.sql", html)
            self.assertIn("PASS=2 WARN=0 ERROR=0 SKIP=0 TOTAL=2", html)
            self.assertNotIn("https://fonts.googleapis.com", html)

    def test_rejects_manifest_artifact_that_escapes_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bundle = write_bundle(root, "preserve")
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["migration"] = "../outside.sql"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "outside.sql").write_text("select 1", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "escapes its directory"):
                load_campaign(bundle)

    def test_loads_manifest_bound_publication_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            manifest_hash = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
            publication = {
                "campaignManifestSha256": manifest_hash,
                "status": "passed",
                "autoMergeEnabled": False,
                "repositories": [
                    {
                        "repository": "itxcrusher/warehouse",
                        "headBranch": "blastradius/propagate-123",
                        "pullRequest": {
                            "number": 1,
                            "url": "https://github.com/itxcrusher/warehouse/pull/1",
                        },
                        "checks": {"status": "passed"},
                    }
                ],
            }
            (bundle / "publication.json").write_text(
                json.dumps(publication),
                encoding="utf-8",
            )

            html = render_replay_html((load_campaign(bundle),))

            self.assertIn("itxcrusher/warehouse", html)
            self.assertIn("https://github.com/itxcrusher/warehouse/pull/1", html)
            self.assertIn("auto-merge disabled", html)

    def test_loads_manifest_bound_fresh_session_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            manifest_hash = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
            decision = {
                "campaignManifestSha256": manifest_hash,
                "decision": "approved-for-review",
                "approvedAt": "2026-07-22T01:02:03+00:00",
                "mergePolicy": "manual-only",
                "assets": ["urn:source", "urn:orders_daily"],
                "verifiedAssets": ["urn:source", "urn:orders_daily"],
                "freshSessionReadback": True,
                "properties": {},
            }
            (bundle / "decision.json").write_text(
                json.dumps(decision),
                encoding="utf-8",
            )

            html = render_replay_html((load_campaign(bundle),))

            self.assertIn("approved-for-review", html)
            self.assertIn("2026-07-22T01:02:03+00:00", html)
            self.assertIn("fresh DataHub client session", html)

    def test_rejects_incomplete_decision_readback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            manifest_hash = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
            decision = {
                "campaignManifestSha256": manifest_hash,
                "assets": ["urn:source", "urn:orders_daily"],
                "verifiedAssets": ["urn:source"],
                "freshSessionReadback": True,
            }
            (bundle / "decision.json").write_text(
                json.dumps(decision),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "verify every covered asset"):
                load_campaign(bundle)

    def test_rejects_publication_evidence_for_a_different_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            (bundle / "publication.json").write_text(
                json.dumps({"campaignManifestSha256": "0" * 64}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                load_campaign(bundle)

    def test_loads_manifest_bound_context_match_and_refusal_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            snapshot = canonical_context_snapshot(
                source_urn="urn:source",
                source_column="user_id",
                max_hops=2,
                schema_fields=("user_id",),
                source_owners=manifest["source"]["owners"],
                downstream=manifest["lineageAssets"],
            )
            manifest["dataHubContext"]["maxHops"] = 2
            manifest["dataHubContext"]["fingerprint"] = context_manifest_record(snapshot)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            captured = captured_context_from_manifest(manifest)
            matched = ContextVerification(
                captured.fingerprint_sha256,
                captured.fingerprint_sha256,
                (),
                "2026-07-22T00:00:00+00:00",
                snapshot,
            )
            drifted_snapshot = json.loads(json.dumps(snapshot))
            drifted_snapshot["source"]["owners"].append(
                {"urn": "urn:ops", "name": "Operations", "type": "TECHNICAL_OWNER"}
            )
            drifted = ContextVerification(
                captured.fingerprint_sha256,
                context_fingerprint(drifted_snapshot),
                ("source ownership changed",),
                "2026-07-22T00:01:00+00:00",
                drifted_snapshot,
            )
            write_context_verification_evidence(
                bundle,
                captured=captured,
                verification=matched,
                scenario="matched",
            )
            write_context_verification_evidence(
                bundle,
                captured=captured,
                verification=drifted,
                scenario="refused-drift",
            )

            html = render_replay_html((load_campaign(bundle),))

            self.assertIn("fresh context match", html)
            self.assertIn("drift refusal", html)
            self.assertIn("source ownership changed", html)

    def test_rejects_context_evidence_for_a_different_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            (bundle / "context-verification.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "campaignManifestSha256": "0" * 64,
                        "fingerprintVersion": 1,
                        "capturedSha256": "0" * 64,
                        "observations": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                load_campaign(bundle)

    def test_escapes_embedded_script_terminators_from_artifact_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            campaign = load_campaign(write_bundle(root, "preserve"))
            campaign["impactReport"] = "</script><script>alert('unsafe')</script>"

            html = render_replay_html((campaign,))

            self.assertNotIn("</script><script>alert('unsafe')</script>", html)
            self.assertIn("<\\/script><script>alert('unsafe')<\\/script>", html)

    def test_write_is_atomic_and_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            campaign = load_campaign(write_bundle(root, "preserve"))
            output = root / "campaign.html"

            written = write_replay((campaign,), output)

            self.assertEqual(written, output.resolve())
            self.assertTrue(output.is_file())
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                write_replay((campaign,), output)
            write_replay((campaign,), output, overwrite=True)
            self.assertIn(
                "BlastRadius campaign review",
                output.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
