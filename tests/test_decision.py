import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    main as decision_main,
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
        datetime.now(timezone.utc).isoformat(),
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
        "downstreamPatches": [
            {
                "repository": "analytics",
                "sourcePath": "models/orders_daily.sql",
                "proposedSha256": "a" * 64,
            },
            {
                "repository": "finance",
                "sourcePath": "models/revenue.sql",
                "proposedSha256": "b" * 64,
            },
        ],
        "contractPatches": [],
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
        "schemaVersion": 1,
        "campaignManifestSha256": digest,
        "status": "passed",
        "mergePolicy": MERGE_POLICY,
        "autoMergeEnabled": False,
        "requiredChecks": ["build"],
        "publishedAt": "2026-07-22T01:02:03+00:00",
        "dataHubContextVerification": {
            "status": "matched",
            "capturedSha256": record["sha256"],
            "freshSha256": record["sha256"],
            "lineageFreshness": matching_lineage().to_dict(),
        },
        "repositories": [
            {
                "label": "analytics",
                "repository": "example/analytics",
                "baseBranch": "main",
                "headBranch": "ripple-proof/propagate-test",
                "commitSha": "a" * 40,
                "files": [
                    {
                        "path": "models/orders_daily.sql",
                        "sha256": "a" * 64,
                    }
                ],
                "autoMergeEnabled": False,
                "pullRequest": {
                    "number": 1,
                    "state": "OPEN",
                    "autoMergeRequest": None,
                    "url": "https://github.com/example/analytics/pull/1",
                    "baseRefName": "main",
                    "headRefName": "ripple-proof/propagate-test",
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
            },
            {
                "label": "finance",
                "repository": "example/finance",
                "baseBranch": "main",
                "headBranch": "ripple-proof/propagate-test",
                "commitSha": "b" * 40,
                "files": [
                    {"path": "models/revenue.sql", "sha256": "b" * 64}
                ],
                "autoMergeEnabled": False,
                "pullRequest": {
                    "number": 2,
                    "state": "OPEN",
                    "autoMergeRequest": None,
                    "url": "https://github.com/example/finance/pull/2",
                    "baseRefName": "main",
                    "headRefName": "ripple-proof/propagate-test",
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
            },
        ],
    }
    (root / "publication.json").write_text(json.dumps(publication), encoding="utf-8")
    return digest


def fresh_publication(bundle: Path):
    path = bundle / "publication.json"
    publication = json.loads(path.read_text(encoding="utf-8"))
    publication["verificationMode"] = "fresh-github"
    publication["refreshedAt"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(publication), encoding="utf-8")
    return publication


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

            with self.assertRaisesRegex(ValueError, "invalid pull request"):
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
                fresh_publication=fresh_publication(bundle),
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
        expected_approved_at = datetime.fromtimestamp(
            123456789 / 1000, tz=timezone.utc
        ).isoformat()
        self.assertEqual(readback.plan.approved_at, expected_approved_at)
        approved_at = next(
            item for item in source.properties
            if item.propertyUrn == PROPERTY_URNS["approved_at"]
        )
        self.assertEqual(approved_at.values, [expected_approved_at])

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
                    fresh_publication=fresh_publication(bundle),
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


    def test_writer_requires_fresh_github_snapshot_before_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            store = SharedStore()

            with self.assertRaisesRegex(DecisionError, "fresh GitHub"):
                write_approved_decision(
                    plan,
                    graph_factory=store.factory,
                    context_verification=matching_context(plan),
                )

            self.assertEqual(store.clients, [])



    def test_stale_or_future_datahub_context_fails_before_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            publication = fresh_publication(bundle)
            store = SharedStore()
            stale_or_future = (
                "2026-01-01T00:00:00+00:00",
                "2099-01-01T00:00:00+00:00",
            )
            for verified_at in stale_or_future:
                with self.subTest(verified_at=verified_at):
                    verification = ContextVerification(
                        plan.datahub_context.fingerprint_sha256,
                        plan.datahub_context.fingerprint_sha256,
                        (),
                        verified_at,
                        lineage_freshness=matching_lineage(),
                    )
                    with self.assertRaisesRegex(DecisionError, "stale"):
                        write_approved_decision(
                            plan,
                            graph_factory=store.factory,
                            context_verification=verification,
                            fresh_publication=publication,
                        )
            self.assertEqual(store.clients, [])

    def test_cli_rereads_datahub_after_github_and_refuses_drift_or_stale(self):
        second_reads = ("drift", "stale")
        for second_read in second_reads:
            with self.subTest(second_read=second_read):
                with tempfile.TemporaryDirectory() as directory:
                    bundle = Path(directory)
                    digest = write_bundle(bundle)
                    plan = prepare_decision(
                        bundle, approved_manifest_sha256=digest
                    )
                    publication = fresh_publication(bundle)
                    first = matching_context(plan)
                    if second_read == "drift":
                        second = ContextVerification(
                            plan.datahub_context.fingerprint_sha256,
                            "f" * 64,
                            ("owners changed",),
                            datetime.now(timezone.utc).isoformat(),
                            lineage_freshness=matching_lineage(),
                        )
                    else:
                        second = ContextVerification(
                            plan.datahub_context.fingerprint_sha256,
                            plan.datahub_context.fingerprint_sha256,
                            (),
                            "2026-01-01T00:00:00+00:00",
                            lineage_freshness=matching_lineage(),
                        )
                    verifier = AsyncMock(side_effect=(first, second))
                    with (
                        patch(
                            "lineage_agent.decision.prepare_publication",
                            return_value=SimpleNamespace(
                                manifest_sha256=digest
                            ),
                        ),
                        patch(
                            "lineage_agent.decision.refresh_publication",
                            return_value=publication,
                        ) as refresh,
                        patch(
                            "lineage_agent.decision.verify_fresh_context",
                            verifier,
                        ),
                        patch(
                            "lineage_agent.decision.DataHubGraph"
                        ) as graph_class,
                        patch("sys.stdout", new=io.StringIO()),
                        patch("sys.stderr", new=io.StringIO()),
                    ):
                        with self.assertRaises(SystemExit) as exit_context:
                            decision_main(
                                [
                                    "--bundle-dir",
                                    str(bundle),
                                    "--write",
                                    "--approve-manifest-sha256",
                                    digest,
                                    "--repo-map",
                                    "analytics=example/analytics",
                                    "--required-check",
                                    "build",
                                ]
                            )

                    self.assertEqual(exit_context.exception.code, 2)
                    self.assertEqual(verifier.await_count, 2)
                    refresh.assert_called_once()
                    graph_class.assert_not_called()

    def test_graph_factory_failure_does_not_freeze_a_fake_write_time(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            publication = fresh_publication(bundle)
            store = SharedStore()
            failed = False

            def factory():
                nonlocal failed
                if not failed:
                    failed = True
                    raise ConnectionError("DataHub is unavailable")
                return store.factory()

            with self.assertRaisesRegex(DecisionError, "could not be opened"):
                write_approved_decision(
                    plan,
                    graph_factory=factory,
                    timestamp_ms=111000,
                    context_verification=matching_context(plan),
                    fresh_publication=publication,
                )

            progress_path = bundle / "decision-progress.json"
            self.assertFalse(progress_path.exists())

            readback = write_approved_decision(
                plan,
                graph_factory=factory,
                timestamp_ms=222000,
                context_verification=matching_context(plan),
                fresh_publication=publication,
            )

            expected = datetime.fromtimestamp(
                222000 / 1000, tz=timezone.utc
            ).isoformat()
            self.assertEqual(readback.plan.approved_at, expected)
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(progress["appliedAtMs"], 222000)
            self.assertEqual(progress["approvedAt"], expected)

    def test_stale_github_refresh_fails_before_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            publication = fresh_publication(bundle)
            publication["refreshedAt"] = "2026-01-01T00:00:00+00:00"
            (bundle / "publication.json").write_text(
                json.dumps(publication), encoding="utf-8"
            )
            store = SharedStore()

            with self.assertRaisesRegex(DecisionError, "stale"):
                write_approved_decision(
                    plan,
                    graph_factory=store.factory,
                    context_verification=matching_context(plan),
                    fresh_publication=publication,
                )

            self.assertEqual(store.clients, [])

    def test_partial_asset_failure_resumes_only_remaining_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            publication = fresh_publication(bundle)
            store = SharedStore()
            store.failed_once = False

            class OneShotFailureGraph(FakeGraph):
                def emit_mcp(self, proposal):
                    if (
                        proposal.entityUrn == DATASET_URN
                        and not self.store.failed_once
                    ):
                        self.store.failed_once = True
                        raise RuntimeError("simulated partial DataHub failure")
                    super().emit_mcp(proposal)

            def factory():
                client = OneShotFailureGraph(store)
                store.clients.append(client)
                return client

            with self.assertRaisesRegex(DecisionError, "partial DataHub failure"):
                write_approved_decision(
                    plan,
                    graph_factory=factory,
                    timestamp_ms=111000,
                    context_verification=matching_context(plan),
                    fresh_publication=publication,
                )

            progress_path = bundle / "decision-progress.json"
            partial = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(partial["writtenAssets"], [SOURCE_URN])
            self.assertEqual(
                partial["remainingAssets"], [DATASET_URN, DASHBOARD_URN]
            )

            readback = write_approved_decision(
                plan,
                graph_factory=factory,
                timestamp_ms=999000,
                context_verification=matching_context(plan),
                fresh_publication=publication,
            )

            resumed_writer = store.clients[1]
            resumed_asset_writes = [
                proposal.entityUrn
                for proposal in resumed_writer.emitted
                if proposal.entityUrn in plan.asset_urns
            ]
            self.assertEqual(
                resumed_asset_writes, [DATASET_URN, DASHBOARD_URN]
            )
            expected_approved_at = datetime.fromtimestamp(
                111000 / 1000, tz=timezone.utc
            ).isoformat()
            self.assertEqual(readback.plan.approved_at, expected_approved_at)
            completed = json.loads(progress_path.read_text(encoding="utf-8"))
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["writtenAssets"], list(plan.asset_urns))
            self.assertEqual(completed["verifiedAssets"], list(plan.asset_urns))

    def test_partial_readback_is_repeated_completely_on_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            digest = write_bundle(bundle)
            plan = prepare_decision(bundle, approved_manifest_sha256=digest)
            publication = fresh_publication(bundle)
            store = SharedStore()
            factory_calls = 0

            class TransientReadGraph(FakeGraph):
                def get_aspect(self, urn, aspect_type):
                    if urn == DASHBOARD_URN:
                        return None
                    return super().get_aspect(urn, aspect_type)

            def factory():
                nonlocal factory_calls
                factory_calls += 1
                client = (
                    TransientReadGraph(store)
                    if factory_calls == 2
                    else FakeGraph(store)
                )
                store.clients.append(client)
                return client

            with self.assertRaisesRegex(DecisionError, "readback did not match"):
                write_approved_decision(
                    plan,
                    graph_factory=factory,
                    timestamp_ms=222000,
                    context_verification=matching_context(plan),
                    fresh_publication=publication,
                )

            partial = json.loads(
                (bundle / "decision-progress.json").read_text(encoding="utf-8")
            )
            self.assertEqual(partial["writtenAssets"], list(plan.asset_urns))
            self.assertEqual(
                partial["verifiedAssets"], [SOURCE_URN, DATASET_URN]
            )

            readback = write_approved_decision(
                plan,
                graph_factory=factory,
                timestamp_ms=999000,
                context_verification=matching_context(plan),
                fresh_publication=publication,
            )

            resumed_writer = store.clients[2]
            resumed_asset_writes = [
                proposal.entityUrn
                for proposal in resumed_writer.emitted
                if proposal.entityUrn in plan.asset_urns
            ]
            self.assertEqual(resumed_asset_writes, [])
            self.assertEqual(
                readback.verified_asset_urns, plan.asset_urns
            )
            self.assertEqual(
                readback.plan.approved_at,
                datetime.fromtimestamp(
                    222000 / 1000, tz=timezone.utc
                ).isoformat(),
            )


if __name__ == "__main__":
    unittest.main()
