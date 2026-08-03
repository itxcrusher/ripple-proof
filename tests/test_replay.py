import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from lineage_agent.context_guard import (
    ContextVerification,
    LineageFreshnessProof,
    canonical_context_snapshot,
    captured_context_from_manifest,
    context_fingerprint,
    context_manifest_record,
    write_context_verification_evidence,
)
from lineage_agent.lineage_freshness import CACHE_BYPASS_METHOD
from lineage_agent.postgres_validation import (
    PostgresValidation,
    PostgresValidationStep,
    write_postgres_evidence,
)
from lineage_agent.replay import load_campaign, render_replay_html, write_replay


def matching_lineage() -> LineageFreshnessProof:
    return LineageFreshnessProof(
        CACHE_BYPASS_METHOD,
        True,
        "a" * 64,
        "a" * 64,
        "b" * 64,
        "b" * 64,
    )


def write_bundle(root: Path, mode: str) -> Path:
    bundle = root / mode
    patches = bundle / "patches"
    proposed = bundle / "proposed"
    patches.mkdir(parents=True)
    proposed.mkdir()
    (bundle / "migration.sql").write_text(
        'ALTER TABLE "analytics"."orders" RENAME COLUMN "user_id" TO "customer_id";\n',
        encoding="utf-8",
    )
    (bundle / "impact-report.md").write_text("# Impact\n", encoding="utf-8")
    (patches / "orders.sql.patch").write_text(
        "--- a/orders.sql\n+++ b/orders.sql\n@@ -1 +1 @@\n-user_id\n+customer_id\n",
        encoding="utf-8",
    )
    proposed_sql = "select customer_id from analytics.orders\n"
    (proposed / "orders.sql").write_text(proposed_sql, encoding="utf-8")
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
                "proposedPath": "proposed/orders.sql",
                "sourceSha256": "a" * 64,
                "proposedSha256": hashlib.sha256(
                    proposed_sql.encode("utf-8")
                ).hexdigest(),
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


def write_publication(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest_hash = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    patch = manifest["downstreamPatches"][0]
    publication = {
        "schemaVersion": 1,
        "campaignManifestSha256": manifest_hash,
        "publishedAt": "2026-07-22T01:00:00+00:00",
        "status": "passed",
        "mergePolicy": "manual-only",
        "autoMergeEnabled": False,
        "requiredChecks": ["build"],
        "repositories": [
            {
                "label": "warehouse",
                "repository": "itxcrusher/warehouse",
                "baseBranch": "main",
                "headBranch": "ripple-proof/propagate-123",
                "commitSha": "a" * 40,
                "files": [
                    {
                        "path": patch["sourcePath"],
                        "sha256": patch["proposedSha256"],
                    }
                ],
                "autoMergeEnabled": False,
                "pullRequest": {
                    "number": 1,
                    "url": "https://github.com/itxcrusher/warehouse/pull/1",
                    "state": "OPEN",
                    "headRefName": "ripple-proof/propagate-123",
                    "baseRefName": "main",
                    "autoMergeRequest": None,
                },
                "checks": {
                    "status": "passed",
                    "requiredChecks": ["build"],
                    "timedOut": False,
                    "waitExitCode": 0,
                    "checks": [
                        {"name": "build", "bucket": "pass", "state": "SUCCESS"}
                    ],
                },
            }
        ],
    }
    (bundle / "publication.json").write_text(
        json.dumps(publication), encoding="utf-8"
    )
    return publication


def write_decision(bundle: Path, publication: dict) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest_hash = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    assets = [manifest["source"]["urn"], *[item["urn"] for item in manifest["lineageAssets"]]]
    prefix = "urn:li:structuredProperty:io.github.itxcrusher.ripple_proof."
    decision = {
        "schemaVersion": 1,
        "campaignManifestSha256": manifest_hash,
        "decision": "approved-for-review",
        "approvedAt": "2026-07-22T01:02:03+00:00",
        "mergePolicy": "manual-only",
        "assets": assets,
        "verifiedAssets": assets,
        "freshSessionReadback": True,
        "properties": {
            f"{prefix}campaignManifestSha256": [manifest_hash],
            f"{prefix}reviewDecision": ["approved-for-review"],
            f"{prefix}approvedAt": ["2026-07-22T01:02:03+00:00"],
            f"{prefix}sourceChange": ["analytics.orders.user_id -> customer_id"],
            f"{prefix}mergePolicy": ["manual-only"],
            f"{prefix}pullRequests": [
                item["pullRequest"]["url"] for item in publication["repositories"]
            ],
        },
    }
    (bundle / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
    return decision


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
            self.assertNotIn("min-width: 320px", html)
            self.assertIn("Executable dbt validation", html)
            self.assertIn("DataHub review decision", html)
            self.assertIn("GitHub delivery", html)
            self.assertIn("Review routing", html)
            self.assertIn("Analytics Engineering", html)
            self.assertIn("Model mapping decisions", html)
            self.assertIn("warehouse/models/orders_daily.sql", html)
            self.assertIn("PASS=2 WARN=0 ERROR=0 SKIP=0 TOTAL=2", html)
            self.assertNotIn("https://fonts.googleapis.com", html)

    def test_loads_coalesced_patch_asset_arrays_with_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            patch = manifest["downstreamPatches"][0]
            patch["assetUrns"] = [patch.pop("assetUrn")]
            patch["assetNames"] = [patch.pop("assetName")]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            campaign = load_campaign(bundle)
            html = render_replay_html((campaign,))

            self.assertEqual(campaign["artifacts"][0]["assetUrns"], ["urn:orders_daily"])
            self.assertEqual(campaign["artifacts"][0]["assetNames"], ["orders_daily"])
            self.assertIn("orders_daily", html)

    def test_loads_and_renders_manifest_bound_postgres_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            migration = (bundle / "migration.sql").read_text(encoding="utf-8")
            step = PostgresValidationStep(
                "migration-transaction",
                ("docker", "exec", "<container>", "psql"),
                0,
                "passed",
                "Applied the rename and rolled it back.",
                "after:customer_id\nrollback:user_id",
            )
            validation = PostgresValidation(
                "passed",
                "postgresql",
                "16.10",
                "postgres:16",
                "disposable container",
                "analytics.orders",
                "user_id",
                "customer_id",
                hashlib.sha256(migration.encode("utf-8")).hexdigest(),
                ("user_id",),
                ("customer_id",),
                ("user_id",),
                (step,),
                "2026-07-22T00:00:00+00:00",
            )
            write_postgres_evidence(bundle, validation)

            html = render_replay_html((load_campaign(bundle),))

            self.assertIn("Disposable PostgreSQL validation", html)
            self.assertIn("After rename:", html)
            self.assertIn("customer_id", html)
            self.assertIn("Passed in a disposable rolled-back database", html)

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
            write_publication(bundle)

            html = render_replay_html((load_campaign(bundle),))

            self.assertIn("itxcrusher/warehouse", html)
            self.assertIn("https://github.com/itxcrusher/warehouse/pull/1", html)
            self.assertIn("auto-merge disabled", html)

    def test_loads_and_renders_manifest_bound_offline_audit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            manifest_hash = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
            verification = {
                "schemaVersion": 1,
                "evidenceMode": "captured-offline",
                "bundleName": "propagate",
                "campaignManifestSha256": manifest_hash,
                "status": "incomplete",
                "summary": "Captured proof is intentionally incomplete.",
                "metrics": {
                    "lineageAssets": 1,
                    "targetRepositories": 1,
                    "generatedFiles": 1,
                    "reviewRoutes": 2,
                    "publishedPullRequests": 0,
                    "decisionAssets": 0,
                },
                "checks": [
                    {
                        "id": check_id,
                        "label": check_id,
                        "status": "incomplete",
                        "detail": "Proof not attached in this fixture.",
                    }
                    for check_id in (
                        "artifact-integrity",
                        "datahub-capture",
                        "lineage-accounting",
                        "review-routing",
                        "review-readiness",
                        "dbt-execution",
                        "postgres-rollback",
                        "context-behavior",
                        "github-delivery",
                        "datahub-decision",
                        "safety-boundary",
                    )
                ],
            }
            (bundle / "verification.json").write_text(
                json.dumps(verification), encoding="utf-8"
            )

            campaign = load_campaign(bundle)
            html = render_replay_html((campaign,))

            self.assertEqual(campaign["verification"]["status"], "incomplete")
            self.assertIn("Offline proof-chain verification", html)
            self.assertIn("artifact-integrity", html)

    def test_rejects_offline_audit_for_another_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            verification = {
                "evidenceMode": "captured-offline",
                "campaignManifestSha256": "0" * 64,
                "status": "passed",
                "checks": [{"status": "passed"}],
            }
            (bundle / "verification.json").write_text(
                json.dumps(verification), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                load_campaign(bundle)

    def test_loads_manifest_bound_fresh_session_decision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            publication = write_publication(bundle)
            write_decision(bundle, publication)

            html = render_replay_html((load_campaign(bundle),))

            self.assertIn("approved-for-review", html)
            self.assertIn("2026-07-22T01:02:03+00:00", html)
            self.assertIn("fresh DataHub client session", html)

    def test_rejects_incomplete_decision_readback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            publication = write_publication(bundle)
            decision = write_decision(bundle, publication)
            decision["verifiedAssets"] = ["urn:source"]
            (bundle / "decision.json").write_text(
                json.dumps(decision),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "complete covered asset set"):
                load_campaign(bundle)

    def test_rejects_publication_evidence_for_a_different_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = write_bundle(Path(temp_dir), "propagate")
            (bundle / "publication.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "campaignManifestSha256": "0" * 64,
                    }
                ),
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
                matching_lineage(),
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
                matching_lineage(),
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

            self.assertIn("cache-bypassed context match", html)
            self.assertIn("owner drift refusal", html)
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
                "RippleProof campaign review",
                output.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
