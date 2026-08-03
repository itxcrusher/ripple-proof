import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lineage_agent.llm import ChangeIntent, interpret_rename
from lineage_agent.main import AssetContext, ChangeRequest, resolve_rename, run


class FakeResponses:
    def __init__(self, intent):
        self.intent = intent
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.intent)


class FakeClient:
    def __init__(self, intent):
        self.responses = FakeResponses(intent)


class LLMInterpretationTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_validated_structured_rename(self):
        client = FakeClient(
            ChangeIntent(
                operation="rename_column",
                source_column="user_id",
                destination_column="customer_id",
                reason="Explicit rename request",
            )
        )

        result = await interpret_rename(
            "Please change the user identifier from user_id to customer_id",
            "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
            client=client,
            model="test-model",
        )

        self.assertEqual(result.old_column, "user_id")
        self.assertEqual(result.new_column, "customer_id")
        self.assertEqual(result.model, "test-model")
        call = client.responses.calls[0]
        self.assertIs(call["text_format"], ChangeIntent)
        self.assertFalse(call["store"])

    async def test_rejects_unsupported_model_intent(self):
        client = FakeClient(
            ChangeIntent(
                operation="unsupported",
                source_column="",
                destination_column="",
                reason="The request adds a column",
            )
        )

        with self.assertRaisesRegex(ValueError, "adds a column"):
            await interpret_rename(
                "add status",
                "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
                client=client,
                model="test-model",
            )

    async def test_rejects_invalid_identifier_from_model(self):
        client = FakeClient(
            ChangeIntent(
                operation="rename_column",
                source_column="user_id; DROP TABLE orders",
                destination_column="customer_id",
                reason="Rename",
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid source"):
            await interpret_rename(
                "rename it",
                "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
                client=client,
                model="test-model",
            )

    async def test_resolve_rename_uses_injected_llm_client(self):
        client = FakeClient(
            ChangeIntent(
                operation="rename_column",
                source_column="user_id",
                destination_column="customer_id",
                reason="Explicit rename request",
            )
        )
        request = ChangeRequest(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
            instruction="Use a friendlier name for user_id: customer_id",
        )

        rename, label = await resolve_rename(
            request,
            "llm",
            llm_client=client,
        )

        self.assertEqual(rename.old_column, "user_id")
        self.assertEqual(rename.new_column, "customer_id")
        self.assertIn("OpenAI", label)

    async def test_ambient_api_key_does_not_select_the_llm(self):
        request = ChangeRequest(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
            instruction="rename user_id to customer_id",
        )
        context = AssetContext(
            schema_fields=("user_id", "order_total"),
            downstream=(),
        )

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-secret"}, clear=False),
            patch(
                "lineage_agent.main.interpret_rename", new_callable=AsyncMock
            ) as interpret,
            patch(
                "lineage_agent.main.read_asset_context",
                new_callable=AsyncMock,
                return_value=context,
            ),
        ):
            result = await run(request)

        interpret.assert_not_awaited()
        self.assertEqual(result.generator, "deterministic parser")

    async def test_auto_mode_cannot_reintroduce_ambient_model_selection(self):
        request = ChangeRequest(
            asset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)",
            instruction="rename user_id to customer_id",
        )

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-secret"}, clear=False),
            patch(
                "lineage_agent.main.interpret_rename", new_callable=AsyncMock
            ) as interpret,
        ):
            with self.assertRaisesRegex(ValueError, "explicitly 'llm'"):
                await resolve_rename(request, "auto")

        interpret.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
