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


class SharedDesignSystemTests(unittest.TestCase):
    """The three reviewer surfaces must read as one product.

    They previously drifted into three palettes: the run and workspace on a
    near-black ground, the old front door on navy, and the generated campaign
    on white. A reviewer walking the flow changed products at every step.
    """

    SURFACES = (
        "examples/run.html",
        "examples/workspace.html",
        "examples/generated-ripple-proof-resolved/campaign.html",
    )

    def test_every_surface_shares_the_ground_and_accent(self):
        for relative in self.SURFACES:
            with self.subTest(surface=relative):
                html = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("#08090a", html)
                self.assertIn("#4cc38a", html)
                self.assertIn('content="dark"', html)

    def test_no_surface_keeps_a_light_ground(self):
        for relative in self.SURFACES:
            with self.subTest(surface=relative):
                html = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
                for retired in ("#fcfdfc", "#f5f7f6", "#0b111b"):
                    self.assertNotIn(retired, html)

    def test_the_flow_is_navigable_in_both_directions(self):
        run = (PROJECT_ROOT / "examples/run.html").read_text(encoding="utf-8")
        workspace = (PROJECT_ROOT / "examples/workspace.html").read_text(encoding="utf-8")

        self.assertIn("workspace.html", run)
        self.assertIn("run.html", workspace)
        for html in (run, workspace):
            self.assertIn("generated-ripple-proof-resolved/campaign.html", html)


class ReviewerSurfaceTests(unittest.TestCase):
    """Guards for the three surfaces a reviewer actually walks.

    The retired examples/index.html is now a redirect into the flow, so these
    assert against the entry point and the review tool instead.
    """

    @classmethod
    def setUpClass(cls):
        cls.run_html = (PROJECT_ROOT / "examples/run.html").read_text(encoding="utf-8")
        cls.workspace = (PROJECT_ROOT / "examples/workspace.html").read_text(encoding="utf-8")
        cls.campaign_html = (
            RICH_RESOLVED_MANIFEST.parent / "campaign.html"
        ).read_text(encoding="utf-8")

    def test_landing_page_explains_the_product_before_any_jargon(self):
        """A stranger must learn what this is before meeting internals.

        The site previously opened on a run log, so the first screen showed
        `list_schema_fields` and elapsed timings before ever saying what
        RippleProof was or who it was for.
        """
        home = (PROJECT_ROOT / "index.html").read_text(encoding="utf-8")
        intro = home.index("Command-line tool for data engineers")
        for jargon in ("list_schema_fields", "get_lineage", "manifest"):
            position = home.find(jargon)
            if position != -1:
                self.assertGreater(position, intro, f"{jargon} appears before the intro")
        self.assertIn("Rename a column without breaking everything downstream", home)
        for section in ("The problem", "How it works", "Get started"):
            self.assertIn(section, home)

    def test_worked_example_is_not_mistaken_for_the_visitor_s_own_data(self):
        self.assertIn("NOT YOUR DATA", self.run_html)
        self.assertIn("not your repositories", self.workspace)

    def test_entry_point_leads_with_the_refusals(self):
        for refusal in (
            "two dbt models claim one asset",
            "column use unproven",
            "the lineage moved after planning",
        ):
            self.assertIn(refusal, self.run_html.lower())
        # The refusals must precede the delivery story: the reason to trust
        # the tool comes before what it produced.
        self.assertLess(
            self.run_html.lower().index("two dbt models claim one asset"),
            self.run_html.lower().index("review-only pull requests"),
        )

    def test_surfaces_make_no_network_calls(self):
        for name, html in (("run", self.run_html), ("workspace", self.workspace)):
            with self.subTest(surface=name):
                self.assertNotIn("<iframe", html.lower())
                self.assertNotIn("fetch(", html)
                self.assertNotIn("<script src=", html.lower())

    def test_surfaces_label_the_evidence_as_captured(self):
        self.assertIn("replays a completed run", self.run_html)
        self.assertIn("does not contact DataHub or GitHub", self.run_html)

    def test_workspace_reaches_the_exact_campaign_sections(self):
        self.assertIn("generated-ripple-proof-resolved/campaign.html", self.workspace)
        for anchor in ("evidence-title", "delivery-title", "decision-title"):
            with self.subTest(anchor=anchor):
                self.assertIn(f'id="{anchor}"', self.campaign_html)

    def test_retired_front_door_redirects_to_the_root_landing_page(self):
        legacy = (PROJECT_ROOT / "examples/index.html").read_text(encoding="utf-8")
        self.assertIn("url=../index.html", legacy)
        self.assertLess(len(legacy), 1200, "the retired page should be a stub, not a second design")

    def test_no_page_is_a_dead_end(self):
        """Every page must offer a way back, including the deepest one."""
        campaign = (
            RICH_RESOLVED_MANIFEST.parent / "campaign.html"
        ).read_text(encoding="utf-8")
        for name, html in (
            ("run", self.run_html),
            ("workspace", self.workspace),
            ("campaign", campaign),
        ):
            with self.subTest(page=name):
                self.assertIn("index.html", html)
                self.assertRegex(html, r'<a[^>]*class="brand"')


if __name__ == "__main__":
    unittest.main()
