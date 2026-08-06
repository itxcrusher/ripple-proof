import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

from lineage_agent.context_guard import context_fingerprint


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_SOURCE = PROJECT_ROOT / "examples/dbt_project/models/orders_daily.sql"
EXAMPLE_PROPOSED = (
    PROJECT_ROOT
    / "examples/generated/proposed/dbt_project/models/orders_daily.sql"
)
EXAMPLE_MANIFEST = PROJECT_ROOT / "examples/generated/manifest.json"
EXAMPLE_PATCH = (
    PROJECT_ROOT
    / "examples/generated/patches/dbt_project/models/orders_daily.sql.patch"
)
PROPAGATE_MANIFEST = PROJECT_ROOT / "examples/generated-propagate/manifest.json"
PROPAGATE_PUBLICATION = PROJECT_ROOT / "examples/generated-propagate/publication.json"
PROPAGATE_DECISION = PROJECT_ROOT / "examples/generated-propagate/decision.json"
RICH_PRESERVE_MANIFEST = (
    PROJECT_ROOT / "examples/generated-rich-preserve/manifest.json"
)
RICH_PROPAGATE_MANIFEST = (
    PROJECT_ROOT / "examples/generated-rich-propagate/manifest.json"
)
RICH_RESOLVED_MANIFEST = (
    PROJECT_ROOT / "examples/generated-ripple-proof-resolved/manifest.json"
)
RICH_RESOLVED_PUBLICATION = RICH_RESOLVED_MANIFEST.parent / "publication.json"
RICH_RESOLVED_DECISION = RICH_RESOLVED_MANIFEST.parent / "decision.json"
RICH_RESOLVED_CONTEXT = RICH_RESOLVED_MANIFEST.parent / "context-verification.json"
RICH_RESOLVED_POSTGRES = RICH_RESOLVED_MANIFEST.parent / "postgres-validation.json"
FRONT_DOOR = PROJECT_ROOT / "examples/index.html"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GeneratedExampleTests(unittest.TestCase):
    def test_richer_preserve_campaign_routes_every_owner_and_builds_cleanly(self):
        manifest = json.loads(RICH_PRESERVE_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 7)
        record = manifest["dataHubContext"]["fingerprint"]
        self.assertEqual(record["sha256"], context_fingerprint(record["snapshot"]))
        self.assertEqual(len(manifest["lineageAssets"]), 5)
        self.assertEqual(len(manifest["targetRepositories"]), 5)
        self.assertEqual(len(manifest["reviewAssignments"]), 6)
        self.assertTrue(
            all(
                assignment["routingStatus"] == "assigned"
                for assignment in manifest["reviewAssignments"]
            )
        )
        self.assertEqual(len(manifest["downstreamPatches"]), 1)
        self.assertEqual(len(manifest["protectedByContract"]), 3)
        self.assertEqual(len(manifest["manualReview"]), 1)
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertTrue(manifest["readyForReview"])
        # This is a preserved historical bundle, captured before the artifact
        # proof step existed, so its final step is still the build summary.
        self.assertTrue(
            all(
                project["steps"][-1]["summary"]
                == "PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4"
                for project in manifest["dbtValidation"]["projects"]
            )
        )

    def test_richer_propagate_campaign_fails_closed_on_ambiguous_consumer(self):
        manifest = json.loads(RICH_PROPAGATE_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 7)
        self.assertEqual(
            next(
                check
                for check in manifest["validations"]
                if check["id"] == "context-fingerprint"
            )["status"],
            "passed",
        )
        self.assertEqual(len(manifest["downstreamPatches"]), 3)
        self.assertEqual(len(manifest["contractPatches"]), 3)
        ambiguity = next(
            item
            for item in manifest["manualReview"]
            if item["assetName"] == "customer_identity_export"
        )
        self.assertIn("Multiple dbt models match", ambiguity["reason"])
        self.assertIn("dbt_operations_primary", ambiguity["reason"])
        self.assertIn("dbt_operations_shadow", ambiguity["reason"])
        self.assertEqual(manifest["dbtValidation"]["status"], "failed")
        self.assertFalse(manifest["readyForReview"])
        self.assertEqual(
            {
                project["repository"]: project["status"]
                for project in manifest["dbtValidation"]["projects"]
            },
            {
                "dbt_finance_project": "passed",
                "dbt_growth_project": "passed",
                "dbt_operations_primary": "failed",
                "dbt_operations_shadow": "passed",
            },
        )
        self.assertEqual(
            next(
                check
                for check in manifest["validations"]
                if check["id"] == "model-mapping"
            )["status"],
            "failed",
        )
        self.assertEqual(
            next(
                check
                for check in manifest["validations"]
                if check["id"] == "dbt-build"
            )["status"],
            "failed",
        )

    def test_richer_propagate_patches_apply_to_supported_repositories(self):
        manifest = json.loads(RICH_PROPAGATE_MANIFEST.read_text(encoding="utf-8"))

        for patch in manifest["downstreamPatches"] + manifest["contractPatches"]:
            completed = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    f"--directory=examples/{patch['repository']}",
                    str(
                        (RICH_PROPAGATE_MANIFEST.parent / patch["diffPath"]).relative_to(
                            PROJECT_ROOT
                        )
                    ),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_explicit_resolution_turns_the_blocked_campaign_green(self):
        manifest = json.loads(RICH_RESOLVED_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 8)
        self.assertEqual(len(manifest["downstreamPatches"]), 4)
        self.assertEqual(len(manifest["contractPatches"]), 4)
        self.assertEqual(
            [item["assetName"] for item in manifest["manualReview"]],
            ["Revenue Dashboard"],
        )
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertTrue(manifest["readyForReview"])
        # Every project must both build cleanly and prove that the exact
        # expected generated nodes executed; a clean build alone is not
        # evidence, because dbt exits zero when it runs nothing.
        for project in manifest["dbtValidation"]["projects"]:
            summaries = [step["summary"] for step in project["steps"]]
            self.assertIn("PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4", summaries)
            self.assertRegex(
                summaries[-1],
                r"Proved [1-9]\d* expected generated model node\(s\)",
            )
        resolution = manifest["modelMappingResolutions"][0]
        self.assertEqual(
            resolution["selectedCandidate"],
            "dbt_operations_primary/models/customer_identity_primary.sql",
        )
        self.assertEqual(
            resolution["unselectedCandidates"],
            ["dbt_operations_shadow/models/customer_identity_shadow.sql"],
        )
        selected_source = (
            PROJECT_ROOT
            / "examples/dbt_operations_primary/models/customer_identity_primary.sql"
        )
        self.assertEqual(
            resolution["selectedSourceSha256"],
            hashlib.sha256(
                selected_source.read_text(encoding="utf-8").encode("utf-8")
            ).hexdigest(),
        )
        recorded_hash = resolution.pop("resolutionSha256")
        expected_hash = hashlib.sha256(
            json.dumps(
                resolution,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(recorded_hash, expected_hash)

    def test_resolved_campaign_patches_apply_to_all_selected_repositories(self):
        manifest = json.loads(RICH_RESOLVED_MANIFEST.read_text(encoding="utf-8"))

        for patch in manifest["downstreamPatches"] + manifest["contractPatches"]:
            completed = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    f"--directory=examples/{patch['repository']}",
                    str(
                        (RICH_RESOLVED_MANIFEST.parent / patch["diffPath"]).relative_to(
                            PROJECT_ROOT
                        )
                    ),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_manifest_hashes_match_checked_in_models(self):
        manifest = json.loads(EXAMPLE_MANIFEST.read_text(encoding="utf-8"))
        patch = manifest["downstreamPatches"][0]

        self.assertEqual(manifest["schemaVersion"], 4)
        self.assertEqual(sha256(EXAMPLE_SOURCE), patch["sourceSha256"])
        self.assertEqual(sha256(EXAMPLE_PROPOSED), patch["proposedSha256"])
        self.assertEqual(patch["preservedOutputAliases"], 1)
        self.assertEqual(patch["remainingExecutableOldIdentifiers"], 1)
        self.assertEqual(manifest["downstreamColumnMode"], "preserve")
        self.assertEqual(
            manifest["targetRepositories"],
            ["dbt_project", "dbt_finance_project"],
        )
        self.assertEqual(manifest["contractPatches"], [])
        self.assertEqual(len(manifest["protectedByContract"]), 1)
        self.assertEqual(
            manifest["protectedByContract"][0]["assetName"],
            "customer_revenue",
        )
        self.assertTrue(manifest["readyForReview"])
        self.assertFalse(manifest["safety"]["targetRepositoriesModified"])
        self.assertFalse(manifest["safety"]["sqlExecuted"])
        self.assertFalse(manifest["safety"]["downstreamOutputContractMayChange"])
        self.assertFalse(manifest["safety"]["transitiveConsumersModified"])
        self.assertEqual(manifest["dataHubContext"]["capture"], "live-mcp")
        self.assertEqual(len(manifest["lineageAssets"]), 3)
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertEqual(manifest["dbtValidation"]["dbtVersion"], "1.9.10")
        self.assertTrue(manifest["safety"]["dbtValidationExecuted"])
        self.assertTrue((PROJECT_ROOT / "examples/generated/campaign.html").is_file())

    def test_checked_in_patch_applies_to_example_project(self):
        completed = subprocess.run(
            [
                "git",
                "apply",
                "--check",
                "--directory=examples/dbt_project",
                str(EXAMPLE_PATCH.relative_to(PROJECT_ROOT)),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_propagate_campaign_patches_apply_to_both_example_projects(self):
        manifest = json.loads(PROPAGATE_MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schemaVersion"], 4)
        self.assertEqual(manifest["downstreamColumnMode"], "propagate")
        self.assertEqual(len(manifest["downstreamPatches"]), 2)
        self.assertEqual(len(manifest["contractPatches"]), 2)
        self.assertTrue(manifest["readyForReview"])
        self.assertEqual(manifest["dbtValidation"]["status"], "passed")
        self.assertEqual(
            manifest["dbtValidation"]["projects"][0]["steps"][-1]["summary"],
            "PASS=4 WARN=0 ERROR=0 SKIP=0 TOTAL=4",
        )

        for patch in manifest["downstreamPatches"] + manifest["contractPatches"]:
            completed = subprocess.run(
                [
                    "git",
                    "apply",
                    "--check",
                    f"--directory=examples/{patch['repository']}",
                    str(
                        (PROPAGATE_MANIFEST.parent / patch["diffPath"]).relative_to(
                            PROJECT_ROOT
                        )
                    ),
                ],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_propagate_publication_is_bound_to_passing_review_prs(self):
        publication = json.loads(PROPAGATE_PUBLICATION.read_text(encoding="utf-8"))

        self.assertEqual(publication["campaignManifestSha256"], sha256(PROPAGATE_MANIFEST))
        self.assertEqual(publication["status"], "passed")
        self.assertEqual(publication["mergePolicy"], "manual-only")
        self.assertFalse(publication["autoMergeEnabled"])
        self.assertEqual(publication["requiredChecks"], ["build"])
        self.assertEqual(len(publication["repositories"]), 2)
        for repository in publication["repositories"]:
            self.assertFalse(repository["autoMergeEnabled"])
            self.assertIsNone(repository["pullRequest"]["autoMergeRequest"])
            self.assertEqual(repository["pullRequest"]["state"], "OPEN")
            self.assertTrue(repository["pullRequest"]["url"].startswith("https://github.com/"))
            self.assertEqual(repository["checks"]["status"], "passed")
            self.assertIn(
                "build",
                [check["name"] for check in repository["checks"]["checks"]],
            )

    def test_propagate_decision_is_bound_to_fresh_datahub_readback(self):
        decision = json.loads(PROPAGATE_DECISION.read_text(encoding="utf-8"))

        self.assertEqual(decision["campaignManifestSha256"], sha256(PROPAGATE_MANIFEST))
        self.assertEqual(decision["decision"], "approved-for-review")
        self.assertEqual(decision["mergePolicy"], "manual-only")
        self.assertTrue(decision["freshSessionReadback"])
        self.assertEqual(set(decision["assets"]), set(decision["verifiedAssets"]))
        self.assertEqual(len(decision["verifiedAssets"]), 4)

    def test_resolved_campaign_delivery_covers_four_review_only_repositories(self):
        publication = json.loads(RICH_RESOLVED_PUBLICATION.read_text(encoding="utf-8"))
        decision = json.loads(RICH_RESOLVED_DECISION.read_text(encoding="utf-8"))

        self.assertEqual(
            publication["campaignManifestSha256"], sha256(RICH_RESOLVED_MANIFEST)
        )
        self.assertEqual(publication["status"], "passed")
        self.assertFalse(publication["autoMergeEnabled"])
        self.assertEqual(len(publication["repositories"]), 4)
        for repository in publication["repositories"]:
            self.assertTrue(
                repository["repository"].startswith("itxcrusher/ripple-proof-dbt-")
            )
            self.assertTrue(repository["headBranch"].startswith("ripple-proof/"))
            self.assertEqual(repository["pullRequest"]["state"], "OPEN")
            self.assertIsNone(repository["pullRequest"]["autoMergeRequest"])
            self.assertEqual(repository["checks"]["status"], "passed")
        self.assertEqual(decision["campaignManifestSha256"], sha256(RICH_RESOLVED_MANIFEST))
        self.assertTrue(decision["freshSessionReadback"])
        self.assertEqual(len(decision["verifiedAssets"]), 6)
        self.assertTrue(
            all(
                property_urn.startswith(
                    "urn:li:structuredProperty:io.github.itxcrusher.ripple_proof."
                )
                for property_urn in decision["properties"]
            )
        )

    def test_resolved_campaign_has_manifest_bound_context_verification_proof(self):
        manifest = json.loads(RICH_RESOLVED_MANIFEST.read_text(encoding="utf-8"))
        evidence = json.loads(RICH_RESOLVED_CONTEXT.read_text(encoding="utf-8"))
        observations = evidence["observations"]

        self.assertEqual(evidence["campaignManifestSha256"], sha256(RICH_RESOLVED_MANIFEST))
        self.assertEqual(
            evidence["capturedSha256"],
            manifest["dataHubContext"]["fingerprint"]["sha256"],
        )
        self.assertEqual(evidence["schemaVersion"], 2)
        self.assertEqual(
            set(observations),
            {"matched", "refused-drift", "refused-lineage-drift"},
        )
        self.assertEqual(observations["matched"]["status"], "matched")
        self.assertEqual(observations["matched"]["differences"], [])
        self.assertTrue(observations["matched"]["lineageFreshness"]["skipCache"])
        self.assertTrue(observations["matched"]["lineageFreshness"]["matched"])
        self.assertEqual(observations["refused-drift"]["status"], "refused")
        self.assertIn("source ownership changed", observations["refused-drift"]["differences"])
        self.assertEqual(
            observations["refused-lineage-drift"]["status"], "refused"
        )
        self.assertFalse(
            observations["refused-lineage-drift"]["lineageFreshness"]["matched"]
        )
        self.assertTrue(
            any(
                "lineage" in difference
                for difference in observations["refused-lineage-drift"]["differences"]
            )
        )

    def test_resolved_campaign_has_disposable_postgres_rename_and_rollback_proof(self):
        evidence = json.loads(RICH_RESOLVED_POSTGRES.read_text(encoding="utf-8"))
        validation = evidence["validation"]

        self.assertEqual(
            evidence["campaignManifestSha256"], sha256(RICH_RESOLVED_MANIFEST)
        )
        self.assertEqual(validation["status"], "passed")
        # The fixture reproduces the full captured source schema, so a
        # destination that already exists can actually collide. Only the
        # renamed column moves; the others must be identical throughout.
        self.assertIn("user_id", validation["beforeColumns"])
        self.assertNotIn("customer_id", validation["beforeColumns"])
        self.assertIn("customer_id", validation["afterColumns"])
        self.assertNotIn("user_id", validation["afterColumns"])
        self.assertEqual(validation["rollbackColumns"], validation["beforeColumns"])
        self.assertEqual(
            [column for column in validation["beforeColumns"] if column != "user_id"],
            [
                column
                for column in validation["afterColumns"]
                if column != "customer_id"
            ],
        )
        self.assertEqual(validation["steps"][0]["outputTail"], "<container-id>")
        self.assertTrue(all(step["status"] == "passed" for step in validation["steps"]))


class PublicFrontDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = FRONT_DOOR.read_text(encoding="utf-8")
        cls.manifest = json.loads(RICH_RESOLVED_MANIFEST.read_text(encoding="utf-8"))
        cls.campaign_html = (
            RICH_RESOLVED_MANIFEST.parent / "campaign.html"
        ).read_text(encoding="utf-8")

    def test_product_introduction_precedes_the_captured_replay(self):
        introduction = "RippleProof is a bounded PostgreSQL column-rename agent"
        replay_label = "Captured campaign replay"

        self.assertIn(introduction, self.html)
        self.assertIn(replay_label, self.html)
        self.assertLess(self.html.index(introduction), self.html.index(replay_label))

    def test_front_door_does_not_overclaim_breakage(self):
        # DataHub proves reachability, not that a reachable asset uses the
        # column. The Looker dashboard in the campaign is the standing example.
        self.assertNotIn("guessing what breaks", self.html)
        self.assertIn("guessing what is at risk", self.html)

    def test_front_door_shows_what_breaks_without_the_refusals(self):
        # A reviewer should see the cost of not having this, not only a list
        # of what was built. Each claim here maps to a checked corpus case or
        # a named regression test, so the section must stay honest about being
        # an adversarial corpus rather than a general benchmark.
        self.assertIn('id="unsafe"', self.html)
        for claim in (
            "Two dbt models claim one asset",
            "Asset reachable, column unproven",
            "Destination column already exists",
            "dbt exits 0 having run nothing",
            "Lineage changed after planning",
        ):
            self.assertIn(claim, self.html)
        self.assertIn("adversarial regression corpus, not a universal benchmark", self.html)
        # It must precede the replay: the reason to trust the tool comes before
        # the walkthrough of what it did.
        self.assertLess(self.html.index('id="unsafe"'), self.html.index('id="replay"'))

    def test_captured_replay_follows_the_complete_campaign_sequence(self):
        panel_ids = [f'replay-step-{index}' for index in range(1, 8)]
        positions = [self.html.index(f'id="{panel_id}"') for panel_id in panel_ids]
        resolution = self.manifest["modelMappingResolutions"][0]

        self.assertEqual(positions, sorted(positions))
        self.assertIn(self.manifest["source"]["instruction"], self.html)
        self.assertIn("DataHub MCP establishes the blast radius", self.html)
        self.assertIn("BLOCKED: model mapping is ambiguous", self.html)
        self.assertIn(resolution["selectedCandidate"], self.html)
        self.assertIn(resolution["unselectedCandidates"][0], self.html)
        self.assertIn(resolution["resolutionSha256"], self.html)
        self.assertIn("Four selected dbt repositories received", self.html)
        self.assertIn("PostgreSQL 16 then observed user_id", self.html)
        self.assertIn("Four pull requests were captured open", self.html)

    def test_replay_is_explicitly_offline_and_does_not_depend_on_an_iframe(self):
        self.assertIn(
            "This is a replay of stored evidence captured from a completed live campaign.",
            self.html,
        )
        self.assertIn("They do not contact DataHub or GitHub", self.html)
        self.assertNotIn("<iframe", self.html.lower())
        self.assertNotIn("fetch(", self.html)
        self.assertNotIn("<script src=", self.html.lower())

    def test_proof_index_links_to_exact_campaign_sections(self):
        required_anchors = (
            "evidence-title",
            "artifacts-title",
            "execution-title",
            "postgres-title",
            "delivery-title",
            "decision-title",
        )

        for anchor in required_anchors:
            with self.subTest(anchor=anchor):
                self.assertIn(
                    "generated-ripple-proof-resolved/campaign.html#" + anchor,
                    self.html,
                )
                self.assertIn(f'id="{anchor}"', self.campaign_html)

    def test_replay_progressively_enhances_accessible_static_content(self):
        panel_tags = re.findall(
            r'<article class="replay-panel"[^>]+role="tabpanel"[^>]*>',
            self.html,
        )

        self.assertEqual(len(panel_tags), 7)
        self.assertTrue(all(" hidden" not in tag for tag in panel_tags))
        self.assertIn('role="tablist"', self.html)
        self.assertEqual(
            len(re.findall(r'<a class="replay-tab"[^>]+role="tab"', self.html)),
            7,
        )
        self.assertIn("ArrowRight", self.html)
        self.assertIn("Home", self.html)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.html)
        self.assertIn('reducedMotion.matches', self.html)

if __name__ == "__main__":
    unittest.main()
