import unittest

from datahub.metadata.schema_classes import (
    AuditStampClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
)

from lineage_agent.main import GeneratedChange, ImpactReport
from lineage_agent.writeback import (
    LEGACY_NOTE_URLS,
    RIPPLE_PROOF_NOTE_URL,
    impact_note_markdown,
    write_impact_notes,
)


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)"
DASHBOARD_URN = "urn:li:dashboard:(looker,revenue_dashboard)"


class FakeGraph:
    def __init__(self, aspects=None):
        self.emitted = []
        self.aspects = aspects or {}

    def get_aspect(self, urn, aspect_type):
        self.asserted_aspect_type = aspect_type
        return self.aspects.get(urn)

    def emit_mcp(self, proposal):
        self.emitted.append(proposal)


def generated_change(downstream):
    return GeneratedChange(
        change_sql=(
            'ALTER TABLE "analytics"."orders" '
            'RENAME COLUMN "user_id" TO "customer_id";'
        ),
        impact=ImpactReport(downstream=downstream, summary="Tracked impact summary."),
        generator="OpenAI test-model structured intent",
    )


class WriteBackTests(unittest.TestCase):
    def test_note_is_explicitly_a_non_executing_proposal(self):
        asset = {
            "urn": DATASET_URN,
            "evidence": "column",
            "columns": ("user_id",),
        }
        note = impact_note_markdown(generated_change([asset]), SOURCE_URN, asset)

        self.assertIn("Confirmed column-level lineage", note)
        self.assertIn("RENAME COLUMN", note)
        self.assertIn("did not execute", note)

    def test_writes_url_keyed_memory_note_for_every_asset(self):
        assets = [
            {
                "urn": DATASET_URN,
                "evidence": "column",
                "columns": ("user_id",),
            },
            {
                "urn": DASHBOARD_URN,
                "evidence": "asset",
                "columns": (),
            },
        ]
        graph = FakeGraph()

        written = write_impact_notes(
            generated_change(assets),
            SOURCE_URN,
            graph=graph,
            timestamp_ms=123456789,
        )

        self.assertEqual(written, (DATASET_URN, DASHBOARD_URN))
        self.assertEqual(len(graph.emitted), 2)
        for proposal in graph.emitted:
            self.assertIsInstance(proposal.aspect, InstitutionalMemoryClass)
            self.assertEqual(len(proposal.aspect.elements), 1)
            note = proposal.aspect.elements[0]
            self.assertEqual(note.url, RIPPLE_PROOF_NOTE_URL)
            self.assertEqual(
                note.createStamp.time,
                123456789,
            )

    def test_repeat_write_preserves_other_links_and_replaces_its_note(self):
        original_stamp = AuditStampClass(time=100, actor="urn:li:corpuser:datahub")
        prior_note = InstitutionalMemoryMetadataClass(
            url=RIPPLE_PROOF_NOTE_URL,
            description="Old RippleProof report",
            createStamp=original_stamp,
        )
        other_link = InstitutionalMemoryMetadataClass(
            url="https://example.com/runbook",
            description="Team runbook",
            createStamp=original_stamp,
        )
        graph = FakeGraph(
            {
                DATASET_URN: InstitutionalMemoryClass(
                    elements=[prior_note, other_link]
                )
            }
        )
        asset = {"urn": DATASET_URN, "evidence": "column", "columns": ("user_id",)}

        write_impact_notes(
            generated_change([asset]),
            SOURCE_URN,
            graph=graph,
            timestamp_ms=200,
        )

        elements = graph.emitted[0].aspect.elements
        self.assertEqual(len(elements), 2)
        self.assertEqual(
            {element.url for element in elements},
            {RIPPLE_PROOF_NOTE_URL, "https://example.com/runbook"},
        )
        updated = next(
            element for element in elements if element.url == RIPPLE_PROOF_NOTE_URL
        )
        self.assertEqual(updated.createStamp.time, 100)
        self.assertEqual(updated.updateStamp.time, 200)
        self.assertIn("Tracked impact summary", updated.description)

    def test_repeat_write_migrates_legacy_note_without_duplication(self):
        original_stamp = AuditStampClass(time=100, actor="urn:li:corpuser:datahub")
        legacy_note = InstitutionalMemoryMetadataClass(
            url=next(iter(LEGACY_NOTE_URLS)),
            description="Legacy report",
            createStamp=original_stamp,
        )
        graph = FakeGraph(
            {
                DATASET_URN: InstitutionalMemoryClass(elements=[legacy_note])
            }
        )
        asset = {"urn": DATASET_URN, "evidence": "column", "columns": ("user_id",)}

        write_impact_notes(
            generated_change([asset]),
            SOURCE_URN,
            graph=graph,
            timestamp_ms=200,
        )

        elements = graph.emitted[0].aspect.elements
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0].url, RIPPLE_PROOF_NOTE_URL)
        self.assertEqual(elements[0].createStamp.time, 100)
        self.assertEqual(elements[0].updateStamp.time, 200)

    def test_empty_impact_does_not_connect_or_write(self):
        result = generated_change([])
        self.assertEqual(write_impact_notes(result, SOURCE_URN), ())


if __name__ == "__main__":
    unittest.main()
