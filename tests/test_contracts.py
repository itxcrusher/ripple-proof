import tempfile
import unittest
from pathlib import Path

from lineage_agent.bundling import ModelPatch
from lineage_agent.contracts import plan_contract_patches


class DbtContractPatchTests(unittest.TestCase):
    def _model_patch(self, root: Path, name: str = "orders_daily") -> ModelPatch:
        model = root / "models" / f"{name}.sql"
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_text("select user_id\n", encoding="utf-8")
        return ModelPatch(
            asset_urn=f"urn:li:dataset:(urn:li:dataPlatform:dbt,{name},PROD)",
            asset_name=name,
            source_path=model,
            relative_path=Path(f"models/{name}.sql"),
            replacement_count=1,
            preserved_output_aliases=0,
            remaining_executable_old_identifiers=0,
            original_sql="select user_id\n",
            proposed_sql="select customer_id\n",
            unified_diff="diff",
            original_sha256="old",
            proposed_sha256="new",
            repository=root.name,
            discovery="filename",
        )

    def test_propagate_mode_renames_declared_column_without_losing_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "warehouse"
            patch = self._model_patch(root)
            schema = root / "models" / "schema.yml"
            schema.write_text(
                "version: 2\n\n"
                "models:\n"
                "  - name: orders_daily\n"
                "    columns:\n"
                "      - name: user_id\n"
                "        description: Stable public identifier.\n"
                "        data_tests:\n"
                "          - not_null\n",
                encoding="utf-8",
            )

            patches, unresolved = plan_contract_patches(
                {root.name: root},
                (patch,),
                "user_id",
                "customer_id",
                "propagate",
            )

            self.assertEqual(unresolved, ())
            self.assertEqual(len(patches), 1)
            self.assertIn("name: customer_id", patches[0].proposed_yaml)
            self.assertIn("description: Stable public identifier.", patches[0].proposed_yaml)
            self.assertIn("- not_null", patches[0].proposed_yaml)
            self.assertEqual(schema.read_text(encoding="utf-8").count("user_id"), 1)

    def test_preserve_mode_does_not_rewrite_the_declared_output_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "warehouse"
            patch = self._model_patch(root)

            patches, unresolved = plan_contract_patches(
                {root.name: root},
                (patch,),
                "user_id",
                "customer_id",
                "preserve",
            )

            self.assertEqual(patches, ())
            self.assertEqual(unresolved, ())

    def test_rejects_a_duplicate_destination_column(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "warehouse"
            patch = self._model_patch(root)
            schema = root / "models" / "schema.yml"
            schema.write_text(
                "version: 2\n"
                "models:\n"
                "  - name: orders_daily\n"
                "    columns:\n"
                "      - name: user_id\n"
                "      - name: customer_id\n",
                encoding="utf-8",
            )

            patches, unresolved = plan_contract_patches(
                {root.name: root},
                (patch,),
                "user_id",
                "customer_id",
                "propagate",
            )

            self.assertEqual(patches, ())
            self.assertIn("already declares", unresolved[0].reason)

    def test_coalesces_multiple_model_edits_in_one_schema_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "warehouse"
            first = self._model_patch(root, "orders_daily")
            second = self._model_patch(root, "customer_revenue")
            schema = root / "models" / "schema.yml"
            schema.write_text(
                "version: 2\n"
                "models:\n"
                "  - name: orders_daily\n"
                "    columns:\n"
                "      - name: user_id\n"
                "        description: Daily contract.\n"
                "  - name: customer_revenue\n"
                "    columns:\n"
                "      - name: user_id\n"
                "        description: Revenue contract.\n",
                encoding="utf-8",
            )

            patches, unresolved = plan_contract_patches(
                {root.name: root},
                (first, second),
                "user_id",
                "customer_id",
                "propagate",
            )

            self.assertEqual(unresolved, ())
            self.assertEqual(len(patches), 1)
            self.assertEqual(
                patches[0].asset_urns,
                (first.asset_urn, second.asset_urn),
            )
            self.assertEqual(patches[0].proposed_yaml.count("name: customer_id"), 2)
            self.assertEqual(patches[0].proposed_yaml.count("name: user_id"), 0)
            self.assertEqual(patches[0].unified_diff.count("+      - name: customer_id"), 2)
            self.assertEqual(schema.read_text(encoding="utf-8").count("user_id"), 2)


if __name__ == "__main__":
    unittest.main()
