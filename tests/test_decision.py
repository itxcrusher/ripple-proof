import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from datahub.metadata.schema_classes import (
    AuditStampClass,
    StructuredPropertiesClass,
    StructuredPropertyValueAssignmentClass,
)

from lineage_agent.context_guard import (
    ContextVerification,
    LineageFreshnessProof,
    canonical_context_snapshot,
    context_manifest_record,
)
from lineage_agent.decision import (
    DECISION_STATUS,
    MERGE_POLICY,
    PROPERTY_URNS,
    DecisionError,
    prepare_decision,
    write_approved_decision,
)
from lineage_agent.lineage_freshness import CACHE_BYPASS_METHOD


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)"
DASHBOARD_URN = "urn:li:dashboard:(looker,revenue_dashboard)"


def matching_context(plan) -> ContextVerification:
    digest = plan.datahub_context.fingerprint_sha256
    return ContextVerification(
        digest,
        digest,
        (),
        "2026-07-22T00:00:00+00:00",
        lineage_freshness=matching_lineage(),
    )


def matching_lineage() -> LineageFreshnessProof:
    return LineageFreshnessProof(
        CACHE_BYPASS_METHOD,
        True,
        "a" * 64,
        "a" * 64,
        "b" * 64,
        "b" * 64,
    )


def write_bundle(root: Path, *, ready=True, dbt_status="passed") -> str:
    downstream = (
        {
            "urn": DATASET_URN,
            "type": "DATASET",
            "name": "orders_daily",
            "platform": "dbt",
            "degree": 1,
            "columns": ("user_id",),
            "evidence": "column",
            "owners": (),
        },
        {
            "urn": DASHBOARD_URN,
            "type": "DASHBOARD",
            "name": "Revenue Dashboard",
            "platform": "looker",
            "degree": 2,
            "columns": (),
            "evidence": "asset",
            "owners": (),
        },
    )
    snapshot = canonical_context_snapshot(
        source_urn=SOURCE_URN,
        source_column="user_id",
        max_hops=2,
        schema_fields=("user_id", "order_total"),
        source_owners=(),
        downstream=downstream,
    )
    record = context_manifest_record(snapshot)
    manifest = {
        "schemaVersion": 7,
        "readyForReview": ready,
        "validations": [{"id": "dbt-build", "status": dbt_status}],
        "dbtValidation": {"status": dbt_status},
        "source": {
            "dataset": "analytics.orders",
            "urn": SOURCE_URN,
            "rename": {"oldColumn": "user_id", "newColumn": "customer_id"},
            "owners": [],
        },
        "lineageAssets": list(downstream),
        "dataHubContext": {
            "capture": "live-mcp",
            "maxHops": 2,
            "fingerprint": record,
        },
    }
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode()
    (root / "manifest.json").write_bytes(manifest_bytes)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    publication = {
        "campaignManifestSha256": digest,
        "status": "passed",
        "mergePolicy": MERGE_POLICY,
        "autoMergeEnabled": False,
        "publishedAt": "2026-07-22T01:02:03+00:00",
        "dataHubContextVerification": {
            "status": "matched",
            "capturedSha256": record["sha256"],
            "freshSha256": record["sha256"],
            "lineageFreshness": matching_lineage().to_dict(),
        },
        "repositories": [
            {
                "pullRequest": {
                    "state": "OPEN",
                    "autoMergeRequest": None,
                    "url": "https://github.com/example/analytics/pull/1",
                },
                "checks": {"status": "passed"},
            },
            {
                "pullRequest": {
                    "state": "OPEN",
                    "autoMergeRequest": None,
                    "url": "https://github.com/example/finance/pull/2",
                },
                "checks": {"status": "passed"},
            },
        ],
    }
    (root / "publication.json").write_text(json.dumps(publication), encoding="utf-8")
    return digest


class SharedStore:
    def __init__(self, aspects=None):
        self.aspects = aspects or {}
        self.clients = []

    def factory(self):
        client = FakeGraph(self)
        self.clients.append(client)
        return client


class FakeGraph:
    def __init__(self, store: SharedStore):
        self.store = store
        self.emitted = []
        self.closed = False

    def get_aspect(self, urn, aspect_type):
        return self.store.aspects.get((urn, aspect_type))

    def emit_mcp(self, proposal):
        self.emitted.append(proposal)
        self.store.aspects[(proposal.entityUrn, type(proposal.aspect))] = proposal.aspect

    def close(self):
        self.closed = True


class DecisionTests(unittest.TestCase):
    def test_dry_run_plan_is_bound_to_manifest_and_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)

            plan = prepare_decision(
                bundle,
                approved_at="2026-07-22T01:02:03+00:00",
            )

        self.assertEqual(plan.manifest_sha256, digest)
        self.assertEqual(
            plan.asset_urns,
            (SOURCE_URN, DATASET_URN, DASHBOARD_URN),
        )
        self.assertEqual(plan.values["decision"], (DECISION_STATUS,))
        self.assertEqual(plan.approved_at, "2026-07-22T01:02:03+00:00")
        self.assertEqual(plan.values["merge_policy"], (MERGE_POLICY,))
        self.assertEqual(len(plan.values["pull_requests"]), 2)

    def test_wrong_approval_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            write_bundle(bundle)

            with self.assertRaisesRegex(DecisionError, "Approval hash does not match"):
                prepare_decision(
                    bundle,
                    approved_manifest_sha256="0" * 64,
                )

    def test_unready_or_failed_campaign_cannot_be_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            write_bundle(bundle, ready=False)
            with self.assertRaisesRegex(ValueError, "not ready"):
                prepare_decision(bundle)

        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            write_bundle(bundle, dbt_status="failed")
            with self.assertRaisesRegex(ValueError, "failed validation"):
                prepare_decision(bundle)

    def test_pull_requests_must_stay_open_passing_and_manual_only(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            write_bundle(bundle)
            publication_path = bundle / "publication.json"
            publication = json.loads(publication_path.read_text(encoding="utf-8"))
            publication["repositories"][0]["pullRequest"]["state"] = "MERGED"
            publication_path.write_text(json.dumps(publication), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "open, passing"):
                prepare_decision(bundle)

    def test_write_preserves_unrelated_properties_and_reads_from_fresh_client(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(
                bundle,
                approved_manifest_sha256=digest,
                approved_at="2026-07-22T01:02:03+00:00",
            )
            unrelated = StructuredPropertyValueAssignmentClass(
                propertyUrn="urn:li:structuredProperty:example.owner",
                values=["data-team"],
                created=AuditStampClass(time=10, actor="urn:li:corpuser:datahub"),
            )
            store = SharedStore(
                {
                    (SOURCE_URN, StructuredPropertiesClass): StructuredPropertiesClass(
                        properties=[unrelated]
                    )
                }
            )

            readback = write_approved_decision(
                plan,
                graph_factory=store.factory,
                timestamp_ms=123456789,
                context_verification=matching_context(plan),
            )

        self.assertEqual(readback.verified_asset_urns, plan.asset_urns)
        self.assertEqual(len(store.clients), 2)
        self.assertTrue(all(client.closed for client in store.clients))
        source = store.aspects[(SOURCE_URN, StructuredPropertiesClass)]
        self.assertIn("urn:li:structuredProperty:example.owner", {
            item.propertyUrn for item in source.properties
        })
        manifest = next(
            item for item in source.properties
            if item.propertyUrn == PROPERTY_URNS["manifest"]
        )
        self.assertEqual(manifest.values, [digest])
        self.assertEqual(manifest.created.time, 123456789)

    def test_fresh_session_mismatch_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(
                bundle,
                approved_manifest_sha256=digest,
            )
            store = SharedStore()

            def lossy_factory():
                graph = store.factory()
                if len(store.clients) == 2:
                    store.aspects.pop((DASHBOARD_URN, StructuredPropertiesClass), None)
                return graph

            with self.assertRaisesRegex(DecisionError, "readback did not match"):
                write_approved_decision(
                    plan,
                    graph_factory=lossy_factory,
                    context_verification=matching_context(plan),
                )

    def test_context_drift_fails_before_opening_a_datahub_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            store = SharedStore()
            verification = ContextVerification(
                plan.datahub_context.fingerprint_sha256,
                "f" * 64,
                (f"downstream assets added: {DASHBOARD_URN}",),
                "2026-07-22T00:00:00+00:00",
                lineage_freshness=matching_lineage(),
            )

            with self.assertRaisesRegex(ValueError, "context drifted"):
                write_approved_decision(
                    plan,
                    graph_factory=store.factory,
                    context_verification=verification,
                )

            self.assertEqual(store.clients, [])


if __name__ == "__main__":
    unittest.main()
