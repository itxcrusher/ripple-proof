import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from lineage_agent.context_guard import (
    ContextGuardError,
    ContextVerification,
    canonical_context_snapshot,
    captured_context_from_manifest,
    context_fingerprint,
    context_manifest_record,
    validate_context_verification_evidence,
    verify_fresh_context,
    write_context_verification_evidence,
)


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
DBT_URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)"


def snapshot(*, schema_fields=("order_total", "user_id"), downstream=None):
    return canonical_context_snapshot(
        source_urn=SOURCE_URN,
        source_column="user_id",
        max_hops=2,
        schema_fields=schema_fields,
        source_owners=(
            {
                "urn": "urn:li:corpGroup:platform-data",
                "name": "Platform Data",
                "type": "Technical Owner",
            },
        ),
        downstream=downstream
        if downstream is not None
        else (
            {
                "urn": DBT_URN,
                "type": "DATASET",
                "name": "orders_daily",
                "platform": "dbt",
                "degree": 1,
                "columns": ("user_id",),
                "evidence": "column",
                "owners": (),
            },
        ),
    )


def manifest(value):
    return {
        "source": {
            "urn": value["source"]["urn"],
            "rename": {"oldColumn": value["source"]["column"]},
            "owners": value["source"]["owners"],
        },
        "lineageAssets": copy.deepcopy(value["lineage"]["downstream"]),
        "dataHubContext": {
            "capture": "live-mcp",
            "maxHops": value["lineage"]["maxHops"],
            "fingerprint": context_manifest_record(value),
        }
    }


def captured(value):
    return captured_context_from_manifest(manifest(value))


class ContextGuardTests(unittest.TestCase):
    def test_canonical_fingerprint_ignores_input_order(self):
        first = snapshot(schema_fields=("order_total", "user_id"))
        second = snapshot(schema_fields=("user_id", "order_total"))

        self.assertEqual(
            context_manifest_record(first)["sha256"],
            context_manifest_record(second)["sha256"],
        )

    def test_unchanged_live_read_matches_captured_context(self):
        expected = snapshot()
        plan = captured(expected)

        async def reader(_urn, _column, _max_hops):
            return SimpleNamespace(
                schema_fields=tuple(expected["source"]["schemaFields"]),
                source_owners=tuple(expected["source"]["owners"]),
                downstream=tuple(expected["lineage"]["downstream"]),
            )

        verification = asyncio.run(verify_fresh_context(plan, reader=reader))

        self.assertTrue(verification.matched)
        self.assertEqual(verification.differences, ())

    def test_schema_and_lineage_drift_are_explained(self):
        plan = captured(snapshot())

        async def reader(_urn, _column, _max_hops):
            return SimpleNamespace(
                schema_fields=("user_id", "order_total", "account_id"),
                source_owners=tuple(plan.snapshot["source"]["owners"]),
                downstream=(),
            )

        verification = asyncio.run(verify_fresh_context(plan, reader=reader))

        self.assertFalse(verification.matched)
        self.assertTrue(any("source schema changed" in item for item in verification.differences))
        self.assertTrue(
            any("downstream assets removed" in item for item in verification.differences)
        )

    def test_manifest_evidence_must_match_its_fingerprinted_snapshot(self):
        value = snapshot()
        campaign = manifest(value)
        campaign["lineageAssets"][0] = {
            **campaign["lineageAssets"][0],
            "name": "tampered_orders_daily",
        }

        with self.assertRaisesRegex(ValueError, "does not match"):
            captured_context_from_manifest(campaign)

    def test_writes_manifest_bound_matched_and_refused_drift_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir)
            expected = snapshot()
            campaign = manifest(expected)
            (bundle / "manifest.json").write_text(
                json.dumps(campaign), encoding="utf-8"
            )
            plan = captured_context_from_manifest(campaign)
            matched = ContextVerification(
                plan.fingerprint_sha256,
                plan.fingerprint_sha256,
                (),
                "2026-07-22T00:00:00+00:00",
                expected,
            )
            drifted_snapshot = snapshot(schema_fields=("order_total", "user_id", "account_id"))
            drifted = ContextVerification(
                plan.fingerprint_sha256,
                context_fingerprint(drifted_snapshot),
                ("source schema changed",),
                "2026-07-22T00:01:00+00:00",
                drifted_snapshot,
            )

            path = write_context_verification_evidence(
                bundle,
                captured=plan,
                verification=matched,
                scenario="matched",
            )
            write_context_verification_evidence(
                bundle,
                captured=plan,
                verification=drifted,
                scenario="refused-drift",
            )
            evidence = json.loads(path.read_text(encoding="utf-8"))
            validated = validate_context_verification_evidence(
                evidence,
                campaign_manifest_sha256=evidence["campaignManifestSha256"],
                captured=plan,
            )

            self.assertEqual(
                set(validated["observations"]), {"matched", "refused-drift"}
            )
            self.assertEqual(validated["observations"]["matched"]["status"], "matched")
            self.assertEqual(
                validated["observations"]["refused-drift"]["status"], "refused"
            )
            with self.assertRaisesRegex(ContextGuardError, "already contains"):
                write_context_verification_evidence(
                    bundle,
                    captured=plan,
                    verification=matched,
                    scenario="matched",
                )

    def test_rejects_evidence_scenario_that_disagrees_with_live_result(self):
        plan = captured(snapshot())
        verification = ContextVerification(
            plan.fingerprint_sha256,
            plan.fingerprint_sha256,
            (),
            "2026-07-22T00:00:00+00:00",
            plan.snapshot,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir)
            (bundle / "manifest.json").write_text(
                json.dumps(manifest(plan.snapshot)), encoding="utf-8"
            )
            with self.assertRaisesRegex(ContextGuardError, "does not match scenario"):
                write_context_verification_evidence(
                    bundle,
                    captured=plan,
                    verification=verification,
                    scenario="refused-drift",
                )


if __name__ == "__main__":
    unittest.main()
