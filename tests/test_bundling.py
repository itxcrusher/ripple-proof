import json
import tempfile
import unittest
from pathlib import Path

from lineage_agent.bundling import plan_model_patches, replace_sql_identifier
from lineage_agent.campaign import (
    AMBIGUOUS_MODEL_PREFIX,
    build_change_bundle,
    parse_model_maps,
    plan_repair_campaign,
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
            "owners": (
                {
                    "urn": "urn:li:corpGroup:analytics-engineering",
                    "name": "Analytics Engineering",
                    "type": "TECHNICAL_OWNER",
                },
            ),
        },
        {
            "urn": DASHBOARD_URN,
            "type": "DASHBOARD",
            "name": "Revenue Dashboard",
            "platform": "Looker",
            "degree": 2,
            "columns": (),
            "evidence": "asset",
            "owners": (
                {
                    "urn": "urn:li:corpGroup:revenue-operations",
                    "name": "Revenue Operations",
                    "type": "TECHNICAL_OWNER",
                },
            ),
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
        source_owners=(
            {
                "urn": "urn:li:corpGroup:platform-data",
                "name": "Platform Data",
                "type": "TECHNICAL_OWNER",
            },
        ),
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

    def test_routes_relation_ambiguous_join_to_manual_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "select\n"
                "    orders.user_id,\n"
                "    profiles.user_id as profile_user_id\n"
                "from analytics.orders as orders\n"
                "join analytics.profiles as profiles\n"
                "    on orders.user_id = profiles.user_id\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
                downstream_mode="propagate",
            )

            self.assertEqual(patches, ())
            self.assertEqual(len(unresolved), 1)
            self.assertIn("cannot prove column ownership", unresolved[0].reason)

    def test_refuses_a_comma_join_whose_alias_is_a_clause_keyword(self):
        # `qualify` is not a PostgreSQL keyword, so it is a legal bare alias.
        # Treating it as a clause terminator ended the multi-relation scan
        # early and let an unrelated relation's column be rewritten.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "select\n"
                "    p.user_id,\n"
                "    qualify.order_total\n"
                "from analytics.orders qualify, analytics.profiles p\n"
                "where qualify.profile_id = p.id\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
                downstream_mode="propagate",
            )

            self.assertEqual(patches, ())
            self.assertEqual(len(unresolved), 1)
            self.assertIn("cannot prove column ownership", unresolved[0].reason)

    def test_preserve_mode_ignores_a_column_inside_a_function_argument(self):
        # A bare `user_id` line nested inside a multi-line function call is an
        # argument, not an output column. Treating it as one produced
        # `coalesce(customer_id as user_id, ...)`, which is not valid SQL, and
        # renamed the real output column, inverting the preserved contract.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "select\n"
                "    coalesce(\n"
                "        user_id,\n"
                "        legacy_user_id\n"
                "    ) as user_id,\n"
                "    order_total\n"
                "from analytics.orders\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
                downstream_mode="preserve",
            )

            for patch in patches:
                self.assertNotIn("as user_id,", patch.proposed_sql.replace(" ", " "))
                self.assertNotIn("customer_id as user_id", patch.proposed_sql)
            if patches:
                self.assertEqual(patches[0].preserved_output_aliases, 0)

    def test_refuses_sql_that_is_not_a_single_select(self):
        # A dbt model should be a SELECT. UPDATE/INSERT/DELETE/MERGE bodies
        # carry their own relation scoping that lexical rewriting cannot prove.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "update analytics.orders\n"
                "set user_id = o.user_id\n"
                "from legacy_accounts o\n"
                "where analytics.orders.id = o.order_id\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
                downstream_mode="propagate",
            )

            self.assertEqual(patches, ())
            self.assertEqual(len(unresolved), 1)
            self.assertIn("cannot prove column ownership", unresolved[0].reason)

    def test_patches_a_single_relation_model_that_uses_extract_from(self):
        # `extract(... from ...)` is a function argument, not a second FROM
        # relation. Counting it refused ordinary analytics models.
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "select\n"
                "    user_id,\n"
                "    extract(year from created_at) as order_year\n"
                "from analytics.orders\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
                downstream_mode="propagate",
            )

            self.assertEqual(unresolved, ())
            self.assertEqual(len(patches), 1)
            self.assertIn("customer_id", patches[0].proposed_sql)
            self.assertIn("extract(year from created_at)", patches[0].proposed_sql)

    def test_routes_cte_projection_to_manual_review_in_preserve_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._project(
                root,
                "with base as (\n"
                "    select user_id from analytics.orders\n"
                ")\n"
                "select user_id from base\n",
            )

            patches, unresolved = plan_model_patches(
                root,
                downstream_assets()[:1],
                "user_id",
                "customer_id",
            )

            self.assertEqual(patches, ())
            self.assertEqual(len(unresolved), 1)
            self.assertIn("CTEs", unresolved[0].reason)

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
                target_repos=target,
                output_dir=output,
            )

            self.assertEqual(model.read_text(encoding="utf-8"), original)
            self.assertEqual(len(bundle.patches), 1)
            self.assertEqual(len(bundle.unresolved), 1)
            self.assertTrue((output / "migration.sql").is_file())
            self.assertTrue((output / "impact-report.md").is_file())
            self.assertTrue((output / "campaign.html").is_file())
            self.assertTrue((output / "patches/models/orders_daily.sql.patch").is_file())
            proposed = (output / "proposed/models/orders_daily.sql").read_text(
                encoding="utf-8"
            )
            self.assertEqual(proposed.count("customer_id"), 3)
            self.assertIn("customer_id as user_id", proposed)
            self.assertIn("-- Keep the user_id output documented.", proposed)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schemaVersion"], 8)
            self.assertEqual(
                manifest["source"]["rename"],
                {"oldColumn": "user_id", "newColumn": "customer_id"},
            )
            self.assertEqual(len(manifest["lineageAssets"]), 2)
            self.assertEqual(manifest["dbtValidation"]["status"], "not-requested")
            self.assertFalse(manifest["safety"]["dbtValidationExecuted"])
            self.assertEqual(
                manifest["postgresValidation"]["status"], "not-requested"
            )
            self.assertFalse(
                manifest["safety"]["postgresMigrationValidationExecuted"]
            )
            self.assertFalse(
                manifest["safety"]["postgresMigrationValidationRequested"]
            )
            self.assertEqual(
                [read["tool"] for read in manifest["dataHubContext"]["reads"]],
                [
                    "list_schema_fields",
                    "get_lineage(column)",
                    "get_lineage(asset)",
                    "get_entities(ownership)",
                ],
            )
            self.assertEqual(
                len(manifest["dataHubContext"]["fingerprint"]["sha256"]),
                64,
            )
            self.assertEqual(len(manifest["reviewAssignments"]), 3)
            self.assertTrue(
                all(
                    route["routingStatus"] == "assigned"
                    for route in manifest["reviewAssignments"]
                )
            )
            self.assertEqual(
                next(
                    check
                    for check in manifest["validations"]
                    if check["id"] == "review-routing"
                )["status"],
                "passed",
            )
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
            self.assertFalse(manifest["safety"]["targetRepositoriesModified"])
            self.assertTrue(manifest["safety"]["humanReviewRequired"])
            self.assertFalse(
                manifest["safety"]["downstreamOutputContractMayChange"]
            )
            self.assertTrue(manifest["readyForReview"])
            self.assertTrue(
                all(
                    check["status"] != "failed"
                    for check in manifest["validations"]
                )
            )
            report = (output / "impact-report.md").read_text(encoding="utf-8")
            self.assertIn("## Generated downstream artifacts", report)
            self.assertIn("Revenue Dashboard", report)
            self.assertIn("preserve the one-hop dbt output contract", report)
            replay = (output / "campaign.html").read_text(encoding="utf-8")
            self.assertIn("RippleProof campaign review", replay)
            self.assertIn("orders_daily", replay)
            self.assertIn("Revenue Dashboard", replay)
            self.assertNotIn("https://fonts.googleapis.com", replay)

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
                    target_repos=target,
                    output_dir=output,
                )

    def test_requested_postgres_validation_failure_blocks_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target-repo"
            model = target / "models" / "orders_daily.sql"
            model.parent.mkdir(parents=True)
            model.write_text(
                "select user_id from {{ source('analytics', 'orders') }}\n",
                encoding="utf-8",
            )
            output = root / "bundle"

            bundle = build_change_bundle(
                result=generated_change(),
                source_urn=SOURCE_URN,
                instruction="rename user_id to customer_id",
                target_repos=target,
                output_dir=output,
                validate_postgres=True,
                docker_executable=root / "missing-docker",
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))

            self.assertEqual(bundle.postgres_validation.status, "failed")
            self.assertFalse(bundle.ready_for_review)
            self.assertEqual(manifest["postgresValidation"]["status"], "failed")
            self.assertTrue(
                manifest["safety"]["postgresMigrationValidationRequested"]
            )
            self.assertFalse(
                manifest["safety"]["postgresMigrationValidationExecuted"]
            )
            self.assertEqual(
                next(
                    check
                    for check in manifest["validations"]
                    if check["id"] == "postgres-migration"
                )["status"],
                "failed",
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
                    target_repos=target,
                    output_dir=target / "generated",
                )


class RepairCampaignPlanningTests(unittest.TestCase):
    def _project(
        self,
        root: Path,
        name: str,
        model_path: str,
        sql: str,
        *,
        model_name: str,
        alias: str,
    ) -> Path:
        project = root / name
        model = project / model_path
        model.parent.mkdir(parents=True)
        model.write_text(sql, encoding="utf-8")
        manifest = {
            "nodes": {
                f"model.{name}.{model_name}": {
                    "resource_type": "model",
                    "name": model_name,
                    "alias": alias,
                    "original_file_path": model_path,
                    "relation_name": f'"analytics"."{alias}"',
                }
            }
        }
        target = project / "target"
        target.mkdir()
        (target / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        schema = model.parent / "schema.yml"
        schema.write_text(
            "version: 2\n\n"
            "models:\n"
            f"  - name: {model_name}\n"
            "    columns:\n"
            "      - name: user_id\n"
            "        description: Stable identifier.\n",
            encoding="utf-8",
        )
        return project

    def _multi_hop_assets(self):
        return [
            {
                "urn": DBT_URN,
                "type": "DATASET",
                "name": "orders_daily_alias",
                "platform": "dbt",
                "degree": 1,
                "columns": ("user_id",),
                "evidence": "column",
            },
            {
                "urn": "urn:li:dataset:(urn:li:dataPlatform:dbt,board_revenue,PROD)",
                "type": "DATASET",
                "name": "board_revenue_alias",
                "platform": "dbt",
                "degree": 2,
                "columns": ("user_id",),
                "evidence": "column",
            },
        ]

    def test_preserve_mode_patches_only_the_boundary_and_protects_transitive_models(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse = self._project(
                root,
                "warehouse",
                "models/core/orders_rollup.sql",
                "select\n    user_id,\n    sum(total) as total\nfrom source\ngroup by user_id\n",
                model_name="orders_rollup",
                alias="orders_daily_alias",
            )
            finance = self._project(
                root,
                "finance-metrics",
                "models/revenue/board_revenue.sql",
                "select\n    user_id,\n    sum(total) as total\n"
                "from {{ ref('orders_rollup') }}\ngroup by user_id\n",
                model_name="board_revenue",
                alias="board_revenue_alias",
            )

            _projects, patches, protected, unresolved = plan_repair_campaign(
                (warehouse, finance),
                self._multi_hop_assets(),
                "user_id",
                "customer_id",
                "preserve",
            )

            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0].repository, "warehouse")
            self.assertEqual(patches[0].discovery, "dbt-manifest")
            self.assertIn("customer_id as user_id", patches[0].proposed_sql)
            self.assertEqual(len(protected), 1)
            self.assertEqual(protected[0].asset_name, "board_revenue_alias")
            self.assertEqual(unresolved, ())

    def test_propagate_mode_patches_confirmed_models_across_repositories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse = self._project(
                root,
                "warehouse",
                "models/core/orders_rollup.sql",
                "select\n    user_id\nfrom source\n",
                model_name="orders_rollup",
                alias="orders_daily_alias",
            )
            finance = self._project(
                root,
                "finance-metrics",
                "models/revenue/board_revenue.sql",
                "select\n    user_id\nfrom upstream\n",
                model_name="board_revenue",
                alias="board_revenue_alias",
            )
            output = root / "bundle"
            result = generated_change()
            result.impact.downstream = self._multi_hop_assets()

            bundle = build_change_bundle(
                result=result,
                source_urn=SOURCE_URN,
                instruction="rename user_id to customer_id",
                target_repos=(warehouse, finance),
                output_dir=output,
                downstream_mode="propagate",
            )

            self.assertEqual(len(bundle.patches), 2)
            self.assertEqual(len(bundle.contract_patches), 2)
            self.assertEqual(bundle.protected, ())
            self.assertEqual(bundle.unresolved, ())
            self.assertTrue(bundle.ready_for_review)
            self.assertTrue(
                (output / "patches/warehouse/models/core/orders_rollup.sql.patch").is_file()
            )
            self.assertTrue(
                (
                    output
                    / "patches/finance-metrics/models/revenue/board_revenue.sql.patch"
                ).is_file()
            )
            self.assertTrue(
                (output / "patches/warehouse/models/core/schema.yml.patch").is_file()
            )
            self.assertTrue(
                (
                    output
                    / "patches/finance-metrics/models/revenue/schema.yml.patch"
                ).is_file()
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["targetRepositories"], ["warehouse", "finance-metrics"]
            )
            self.assertEqual(len(manifest["contractPatches"]), 2)
            self.assertTrue(manifest["safety"]["transitiveConsumersModified"])

    def test_bundle_emits_one_complete_patch_for_a_shared_schema_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse = root / "warehouse"
            models = warehouse / "models"
            models.mkdir(parents=True)
            (models / "orders_daily.sql").write_text(
                "select user_id from upstream\n", encoding="utf-8"
            )
            (models / "customer_revenue.sql").write_text(
                "select user_id from upstream\n", encoding="utf-8"
            )
            schema = models / "schema.yml"
            schema.write_text(
                "version: 2\n"
                "models:\n"
                "  - name: orders_daily\n"
                "    columns:\n"
                "      - name: user_id\n"
                "  - name: customer_revenue\n"
                "    columns:\n"
                "      - name: user_id\n",
                encoding="utf-8",
            )
            target = warehouse / "target"
            target.mkdir()
            (target / "manifest.json").write_text(
                json.dumps(
                    {
                        "nodes": {
                            "model.warehouse.orders_daily": {
                                "resource_type": "model",
                                "name": "orders_daily",
                                "alias": "orders_daily",
                                "original_file_path": "models/orders_daily.sql",
                            },
                            "model.warehouse.customer_revenue": {
                                "resource_type": "model",
                                "name": "customer_revenue",
                                "alias": "customer_revenue",
                                "original_file_path": "models/customer_revenue.sql",
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            first_urn = DBT_URN
            second_urn = (
                "urn:li:dataset:(urn:li:dataPlatform:dbt,customer_revenue,PROD)"
            )
            assets = [
                {
                    "urn": first_urn,
                    "type": "DATASET",
                    "name": "orders_daily",
                    "platform": "dbt",
                    "degree": 1,
                    "columns": ("user_id",),
                    "evidence": "column",
                },
                {
                    "urn": second_urn,
                    "type": "DATASET",
                    "name": "customer_revenue",
                    "platform": "dbt",
                    "degree": 1,
                    "columns": ("user_id",),
                    "evidence": "column",
                },
            ]
            result = generated_change()
            result.impact.downstream = assets
            output = root / "bundle"

            bundle = build_change_bundle(
                result=result,
                source_urn=SOURCE_URN,
                instruction="rename user_id to customer_id",
                target_repos=warehouse,
                output_dir=output,
                downstream_mode="propagate",
            )

            self.assertTrue(bundle.ready_for_review)
            self.assertEqual(len(bundle.contract_patches), 1)
            self.assertEqual(
                bundle.contract_patches[0].asset_urns,
                (first_urn, second_urn),
            )
            proposed = output / "proposed/models/schema.yml"
            diff = output / "patches/models/schema.yml.patch"
            self.assertEqual(proposed.read_text(encoding="utf-8").count("customer_id"), 2)
            self.assertEqual(diff.read_text(encoding="utf-8").count("+      - name: customer_id"), 2)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["contractPatches"]), 1)
            self.assertEqual(
                manifest["contractPatches"][0]["assetUrns"],
                [first_urn, second_urn],
            )
            self.assertEqual(schema.read_text(encoding="utf-8").count("user_id"), 2)

    def test_explicit_model_map_resolves_and_hashes_an_ambiguous_asset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            primary = self._project(
                root,
                "primary",
                "models/identity_primary.sql",
                "select user_id from upstream\n",
                model_name="identity_primary",
                alias="orders_daily_alias",
            )
            shadow = self._project(
                root,
                "shadow",
                "models/identity_shadow.sql",
                "select 'shadow-only' as user_id\n",
                model_name="identity_shadow",
                alias="orders_daily_alias",
            )
            result = generated_change()
            result.impact.downstream = [self._multi_hop_assets()[0]]

            blocked = build_change_bundle(
                result=result,
                source_urn=SOURCE_URN,
                instruction="rename user_id to customer_id",
                target_repos=(primary, shadow),
                output_dir=root / "blocked",
                downstream_mode="propagate",
            )
            self.assertFalse(blocked.ready_for_review)
            self.assertEqual(blocked.patches, ())
            self.assertEqual(
                next(
                    check
                    for check in blocked.validations
                    if check.check_id == "model-mapping"
                ).status,
                "failed",
            )

            resolved = build_change_bundle(
                result=result,
                source_urn=SOURCE_URN,
                instruction="rename user_id to customer_id",
                target_repos=(primary, shadow),
                output_dir=root / "resolved",
                downstream_mode="propagate",
                model_maps={
                    DBT_URN: "primary/models/identity_primary.sql",
                },
            )
            self.assertTrue(resolved.ready_for_review)
            self.assertEqual(len(resolved.patches), 1)
            self.assertEqual(len(resolved.model_mapping_resolutions), 1)
            manifest = json.loads(
                (root / "resolved/manifest.json").read_text(encoding="utf-8")
            )
            decision = manifest["modelMappingResolutions"][0]
            self.assertEqual(
                decision["selectedCandidate"],
                "primary/models/identity_primary.sql",
            )
            self.assertEqual(
                decision["unselectedCandidates"],
                ["shadow/models/identity_shadow.sql"],
            )
            self.assertEqual(len(decision["resolutionSha256"]), 64)

    def test_manifest_candidate_does_not_hide_filename_candidate_in_another_repo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest_repo = self._project(
                root,
                "manifest-repo",
                "models/manifest_model.sql",
                "select user_id from upstream\n",
                model_name="manifest_model",
                alias="orders_daily_alias",
            )
            filename_repo = self._project(
                root,
                "filename-repo",
                "models/unrelated.sql",
                "select 1 as unrelated\n",
                model_name="unrelated",
                alias="unrelated",
            )
            hidden = filename_repo / "models" / "orders_daily_alias.sql"
            hidden.write_text("select user_id from upstream\n", encoding="utf-8")

            _projects, patches, protected, unresolved = plan_repair_campaign(
                (manifest_repo, filename_repo),
                [self._multi_hop_assets()[0]],
                "user_id",
                "customer_id",
                "propagate",
            )

            self.assertEqual(patches, ())
            self.assertEqual(protected, ())
            self.assertEqual(len(unresolved), 1)
            self.assertTrue(unresolved[0].reason.startswith(AMBIGUOUS_MODEL_PREFIX))
            self.assertIn("manifest-repo/models/manifest_model.sql", unresolved[0].reason)
            self.assertIn("filename-repo/models/orders_daily_alias.sql", unresolved[0].reason)

    def test_model_map_must_select_an_exact_discovered_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self._project(
                root,
                "first",
                "models/identity_first.sql",
                "select user_id from upstream\n",
                model_name="identity_first",
                alias="orders_daily_alias",
            )
            second = self._project(
                root,
                "second",
                "models/identity_second.sql",
                "select user_id from upstream\n",
                model_name="identity_second",
                alias="orders_daily_alias",
            )

            with self.assertRaisesRegex(ValueError, "exactly one discovered candidate"):
                plan_repair_campaign(
                    (first, second),
                    [self._multi_hop_assets()[0]],
                    "user_id",
                    "customer_id",
                    "propagate",
                    {DBT_URN: "second/models/not-the-model.sql"},
                )

    def test_model_map_parser_is_strict(self):
        self.assertEqual(
            parse_model_maps((f"{DBT_URN}=warehouse/models/orders_daily.sql",)),
            {DBT_URN: "warehouse/models/orders_daily.sql"},
        )
        with self.assertRaisesRegex(ValueError, "Duplicate model mapping"):
            parse_model_maps(
                (
                    f"{DBT_URN}=warehouse/models/orders_daily.sql",
                    f"{DBT_URN}=shadow/models/orders_daily.sql",
                )
            )

    def test_preserve_mode_does_not_claim_protection_without_a_hop_distance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse = self._project(
                root,
                "warehouse",
                "models/core/orders_rollup.sql",
                "select\n    user_id\nfrom source\n",
                model_name="orders_rollup",
                alias="orders_daily_alias",
            )
            asset = self._multi_hop_assets()[0]
            asset["degree"] = None

            _projects, patches, protected, unresolved = plan_repair_campaign(
                warehouse,
                [asset],
                "user_id",
                "customer_id",
                "preserve",
            )

            self.assertEqual(patches, ())
            self.assertEqual(protected, ())
            self.assertIn("hop distance", unresolved[0].reason)

    def test_preserve_mode_does_not_use_an_unrelated_same_degree_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse = self._project(
                root,
                "warehouse",
                "models/orders_rollup.sql",
                "select\n    user_id\nfrom source\n",
                model_name="orders_rollup",
                alias="orders_daily_alias",
            )
            finance = self._project(
                root,
                "finance",
                "models/board_revenue.sql",
                "select\n    user_id\nfrom {{ ref('unrelated_boundary') }}\n",
                model_name="board_revenue",
                alias="board_revenue_alias",
            )
            assets = self._multi_hop_assets()
            assets.insert(
                1,
                {
                    "urn": "urn:li:dataset:(urn:li:dataPlatform:snowflake,unrelated_boundary,PROD)",
                    "type": "DATASET",
                    "name": "unrelated_boundary",
                    "platform": "snowflake",
                    "degree": 1,
                    "columns": ("user_id",),
                    "evidence": "column",
                },
            )

            _projects, patches, protected, unresolved = plan_repair_campaign(
                (warehouse, finance),
                assets,
                "user_id",
                "customer_id",
                "preserve",
            )

            self.assertEqual(len(patches), 1)
            self.assertEqual(protected, ())
            reasons = {asset.asset_name: asset.reason for asset in unresolved}
            self.assertIn("confirmed dbt datasets only", reasons["unrelated_boundary"])
            self.assertIn("exact local dbt ref path", reasons["board_revenue_alias"])


if __name__ == "__main__":
    unittest.main()
