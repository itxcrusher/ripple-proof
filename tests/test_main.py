import unittest

from lineage_agent.main import (
    ChangeRequest,
    RenameChange,
    _downstream_assets,
    _field_names,
    _known_error_message,
    _merge_downstream_assets,
    _postgres_table_name,
    generate_change,
    parse_rename,
)


ASSET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"


class RenameParsingTests(unittest.TestCase):
    def test_parses_supported_rename(self) -> None:
        self.assertEqual(
            parse_rename("rename user_id to customer_id"),
            RenameChange(old_column="user_id", new_column="customer_id"),
        )

    def test_rejects_unsupported_instruction(self) -> None:
        with self.assertRaisesRegex(ValueError, "supports only"):
            parse_rename("drop user_id")

    def test_rejects_noop_rename(self) -> None:
        with self.assertRaisesRegex(ValueError, "must differ"):
            parse_rename("rename user_id to user_id")


class DataHubResponseTests(unittest.TestCase):
    def test_extracts_schema_field_names(self) -> None:
        self.assertEqual(
            _field_names({"fields": [{"fieldPath": "user_id"}, {"fieldPath": "total"}]}),
            ("user_id", "total"),
        )

    def test_extracts_column_level_lineage(self) -> None:
        payload = {
            "downstreams": {
                "searchResults": [
                    {
                        "entity": {
                            "urn": "urn:downstream",
                            "type": "DATASET",
                            "properties": {"name": "orders_daily"},
                            "platform": {"name": "dbt"},
                        },
                        "degree": 1,
                        "lineageColumns": ["user_id"],
                    }
                ]
            }
        }
        self.assertEqual(
            _downstream_assets(payload, "column")[0]["columns"],
            ("user_id",),
        )

    def test_merge_prefers_column_evidence_and_keeps_wider_assets(self) -> None:
        column_asset = ({"urn": "urn:dataset", "evidence": "column"},)
        asset_level = (
            {"urn": "urn:dataset", "evidence": "asset"},
            {"urn": "urn:dashboard", "evidence": "asset"},
        )

        merged = _merge_downstream_assets(column_asset, asset_level)

        self.assertEqual([item["urn"] for item in merged], ["urn:dataset", "urn:dashboard"])
        self.assertEqual(merged[0]["evidence"], "column")


class ChangeGenerationTests(unittest.TestCase):
    def test_quotes_postgres_table_name(self) -> None:
        self.assertEqual(_postgres_table_name(ASSET_URN), '"analytics"."orders"')

    def test_generates_rename_with_tracked_column_impact(self) -> None:
        request = ChangeRequest(ASSET_URN, "rename user_id to customer_id")
        downstream = [
            {
                "urn": "urn:downstream",
                "type": "DATASET",
                "name": "orders_daily",
                "platform": "dbt",
                "degree": 1,
                "columns": ("user_id",),
                "evidence": "column",
            }
        ]
        result = generate_change(
            request,
            RenameChange("user_id", "customer_id"),
            downstream,
        )

        self.assertEqual(
            result.change_sql,
            'ALTER TABLE "analytics"."orders" RENAME COLUMN "user_id" TO "customer_id";',
        )
        self.assertEqual(result.rename, RenameChange("user_id", "customer_id"))
        self.assertIn("column-level; derived column(s): user_id", result.impact.summary)
        self.assertIn("Remediation plan", result.impact.summary)
        self.assertIn(
            "Update the upstream reference from `user_id` to `customer_id`",
            result.impact.summary,
        )
        self.assertIn(
            "Confirm whether downstream column(s) `user_id` should keep",
            result.impact.summary,
        )
        self.assertIn("cannot detect dependencies missing from DataHub", result.impact.summary)

    def test_asset_level_impact_recommends_review_not_a_fabricated_fix(self) -> None:
        request = ChangeRequest(ASSET_URN, "rename user_id to customer_id")
        downstream = [
            {
                "urn": "urn:dashboard",
                "type": "DASHBOARD",
                "name": "Revenue Dashboard",
                "platform": "looker",
                "degree": 2,
                "columns": (),
                "evidence": "asset",
            }
        ]

        result = generate_change(
            request,
            RenameChange("user_id", "customer_id"),
            downstream,
        )

        self.assertIn(
            "Inspect this asset for references to `user_id`",
            result.impact.summary,
        )
        self.assertIn("column usage is unconfirmed", result.impact.summary)

    def test_empty_lineage_is_not_called_safe(self) -> None:
        request = ChangeRequest(ASSET_URN, "rename user_id to customer_id")
        result = generate_change(
            request,
            RenameChange("user_id", "customer_id"),
            [],
        )

        self.assertNotIn("safe to proceed", result.impact.summary.lower())
        self.assertIn("not proof", result.impact.summary.lower())

    def test_unwraps_expected_error_from_async_exception_group(self) -> None:
        grouped = ExceptionGroup("task group", [ValueError("column is missing")])

        self.assertEqual(_known_error_message(grouped), "column is missing")


if __name__ == "__main__":
    unittest.main()
