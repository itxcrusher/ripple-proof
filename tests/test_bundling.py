import json
import tempfile
import unittest
from pathlib import Path

from lineage_agent.bundling import (
    build_change_bundle,
    plan_model_patches,
    replace_sql_identifier,
)
from lineage_agent.main import GeneratedChange, ImpactReport, RenameChange


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
DBT_URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)"
DASHBOARD_URN = "urn:li:dashboard:(looker,revenue_dashboard)"


def downstream_assets():
    return [
        {
            "urn": DBT_URN,
            "type": "DATASET",
            "name": "orders_daily",
            "platform": "dbt",
            "degree": 1,
            "columns": ("user_id",),
            "evidence": "column",
        },
        {
            "urn": DASHBOARD_URN,
            "type": "DASHBOARD",
            "name": "Revenue Dashboard",
            "platform": "Looker",
            "degree": 2,
            "columns": (),
            "evidence": "asset",
        },
    ]


def generated_change():
    return GeneratedChange(
        change_sql=(
            'ALTER TABLE "analytics"."orders" '
            'RENAME COLUMN "user_id" TO "customer_id";'
        ),
        impact=ImpactReport(
            downstream=downstream_assets(),
            summary="Tracked impact summary.",
        ),
        rename=RenameChange("user_id", "customer_id"),
        generator="deterministic parser",
    )


class SqlIdentifierReplacementTests(unittest.TestCase):
    def test_replaces_executable_identifiers_only(self):
        sql = (
            "select user_id, t.user_id, \"user_id\"\n"
            "from analytics.orders t\n"
            "where user_id is not null\n"
            "-- user_id in a comment\n"
            "and 'user_id' = 'user_id'\n"
            "/* user_id in a block comment */\n"
            "{{ config(alias='user_id') }}\n"
            "$tag$user_id$tag$\n"
        )

        proposed, count = replace_sql_identifier(sql, "user_id", "customer_id")

        self.assertEqual(count, 4)
        self.assertIn("select customer_id, t.customer_id, \"customer_id\"", proposed)
        self.assertIn("where customer_id is not null", proposed)
        self.assertIn("-- user_id in a comment", proposed)
        self.assertIn("'user_id' = 'user_id'", proposed)
        self.assertIn("/* user_id in a block comment */", proposed)
        self.assertIn("{{ config(alias='user_id') }}", proposed)
        self.assertIn("$tag$user_id$tag$", proposed)

    def test_does_not_replace_partial_identifiers(self):
        proposed, count = replace_sql_identifier(
            "select user_id_hash from users_by_user_id",
            "user_id",
            "customer_id",
        )

        self.assertEqual(count, 0)
        self.assertEqual(proposed, "select user_id_hash from users_by_user_id")

    def test_rejects_unterminated_sql_string(self):
        with self.assertRaisesRegex(ValueError, "Unterminated"):
            replace_sql_identifier("select 'user_id", "user_id", "customer_id")


class ModelPatchPlanningTests(unittest.TestCase):
    def _project(self, root: Path, sql: str) -> Path:
        models = root / "models"
        models.mkdir(parents=True)
        model = models / "orders_daily.sql"
        model.write_text(sql, encoding="utf-8")
        return model

    def test_plans_patch_for_confirmed_dbt_model_and_flags_dashboard(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model = self._project(
                root,
                "select\n    user_id,\n    sum(order_total) as total\n"
                "from analytics.orders\ngroup by user_id\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets(),
                "user_id",
                "customer_id",
            )

            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0].source_path, model)
            self.assertEqual(patches[0].replacement_count, 2)
            self.assertEqual(patches[0].preserved_output_aliases, 1)
            self.assertIn(
                "+    customer_id as user_id,",
                patches[0].unified_diff,
            )
            self.assertIn("+group by customer_id", patches[0].unified_diff)
            self.assertEqual(len(unresolved), 1)
            self.assertIn("asset-level reachability", unresolved[0].reason)

    def test_propagate_mode_renames_downstream_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "select\n    user_id\nfrom analytics.orders\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
                downstream_mode="propagate",
            )

            self.assertEqual(unresolved, ())
            self.assertEqual(patches[0].preserved_output_aliases, 0)
            self.assertIn("customer_id", patches[0].proposed_sql)
            self.assertNotIn("as user_id", patches[0].proposed_sql)

    def test_preserve_mode_handles_qualified_quoted_projection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                'select\n    "orders"."user_id",\n    count(*) as total\n'
                'from analytics.orders\ngroup by "orders"."user_id"\n',
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
            )

            self.assertEqual(unresolved, ())
            self.assertIn(
                '"orders"."customer_id" as "user_id",',
                patches[0].proposed_sql,
            )
            self.assertIn(
                'group by "orders"."customer_id"',
                patches[0].proposed_sql,
            )

    def test_preserve_mode_rejects_complex_output_expression(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "select\n    coalesce(user_id, 'unknown') as user_id\n"
                "from analytics.orders\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
            )

            self.assertEqual(patches, ())
            self.assertIn("not a single unambiguous identifier", unresolved[0].reason)

    def test_flags_ambiguous_model_paths_instead_of_guessing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root, "select user_id\n")
            duplicate = root / "models" / "finance"
            duplicate.mkdir()
            (duplicate / "orders_daily.sql").write_text(
                "select user_id\n",
                encoding="utf-8",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
            )

            self.assertEqual(patches, ())
            self.assertIn("Multiple dbt model files", unresolved[0].reason)

    def test_flags_model_without_executable_reference(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(root, "select 'user_id' as label\n")

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
            )

            self.assertEqual(patches, ())
            self.assertIn("no executable", unresolved[0].reason)


class ChangeBundleTests(unittest.TestCase):
    def test_writes_reviewable_bundle_without_modifying_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target-repo"
            model = target / "models" / "orders_daily.sql"
            model.parent.mkdir(parents=True)
            original = (
                "{{ config(materialized='table') }}\n\n"
                "-- Keep the user_id output documented.\n"
                "select\n"
                "    user_id,\n"
                "    sum(order_total) as total_revenue\n"
                "from {{ source('analytics', 'orders') }}\n"
                "where user_id is not null\n"
                "group by user_id\n"
            )
            model.write_text(original, encoding="utf-8")
            output = root / "bundle"

            bundle = build_change_bundle(
                result=generated_change(),
                source_urn=SOURCE_URN,
                instruction="rename user_id to customer_id",
                target_repo=target,
                output_dir=output,
            )

            self.assertEqual(model.read_text(encoding="utf-8"), original)
            self.assertEqual(len(bundle.patches), 1)
            self.assertEqual(len(bundle.unresolved), 1)
            self.assertTrue((output / "migration.sql").is_file())
            self.assertTrue((output / "impact-report.md").is_file())
            self.assertTrue((output / "patches/models/orders_daily.sql.patch").is_file())
            proposed = (output / "proposed/models/orders_daily.sql").read_text(
                encoding="utf-8"
            )
            self.assertEqual(proposed.count("customer_id"), 3)
            self.assertIn("customer_id as user_id", proposed)
            self.assertIn("-- Keep the user_id output documented.", proposed)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schemaVersion"], 1)
            self.assertEqual(manifest["downstreamPatches"][0]["replacementCount"], 3)
            self.assertEqual(
                manifest["downstreamPatches"][0]["preservedOutputAliases"],
                1,
            )
            self.assertEqual(
                manifest["downstreamPatches"][0][
                    "remainingExecutableOldIdentifiers"
                ],
                1,
            )
            self.assertEqual(manifest["downstreamColumnMode"], "preserve")
            self.assertFalse(manifest["safety"]["sqlExecuted"])
            self.assertFalse(manifest["safety"]["targetRepositoryModified"])
            self.assertTrue(manifest["safety"]["humanReviewRequired"])
            self.assertFalse(
                manifest["safety"]["downstreamOutputContractMayChange"]
            )
            report = (output / "impact-report.md").read_text(encoding="utf-8")
            self.assertIn("## Generated downstream artifacts", report)
            self.assertIn("Revenue Dashboard", report)
            self.assertIn("preserve confirmed downstream output names", report)

    def test_refuses_to_overwrite_existing_bundle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target-repo"
            model = target / "models" / "orders_daily.sql"
            model.parent.mkdir(parents=True)
            model.write_text("select user_id\n", encoding="utf-8")
            output = root / "bundle"
            output.mkdir()

            with self.assertRaisesRegex(FileExistsError, "already exists"):
                build_change_bundle(
                    result=generated_change(),
                    source_urn=SOURCE_URN,
                    instruction="rename user_id to customer_id",
                    target_repo=target,
                    output_dir=output,
                )

    def test_refuses_to_write_bundle_inside_target_repository(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "target-repo"
            model = target / "models" / "orders_daily.sql"
            model.parent.mkdir(parents=True)
            model.write_text("select user_id\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "outside the target repository"):
                build_change_bundle(
                    result=generated_change(),
                    source_urn=SOURCE_URN,
                    instruction="rename user_id to customer_id",
                    target_repo=target,
                    output_dir=target / "generated",
                )


if __name__ == "__main__":
    unittest.main()
