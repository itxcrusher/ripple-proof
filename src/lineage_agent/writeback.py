"""Write BlastRadius impact reports back to DataHub documentation metadata."""

import os
import time
from typing import Any

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
)
from dotenv import load_dotenv


BLASTRADIUS_ACTOR_URN = "urn:li:corpuser:blastradius"
BLASTRADIUS_NOTE_URL = "https://github.com/itxcrusher/blastradius#impact-report"


def _asset_evidence(asset: dict[str, Any]) -> str:
    if asset.get("evidence") == "column":
        columns = ", ".join(asset.get("columns") or ()) or "unspecified"
        return f"Confirmed column-level lineage through: `{columns}`."
    return "Asset-level reachability only; column usage remains unconfirmed."


def impact_note_markdown(
    result: Any,
    source_urn: str,
    affected_asset: dict[str, Any],
) -> str:
    """Build the visible DataHub institutional-memory note for one asset."""
    return (
        "## BlastRadius proposed-change impact\n\n"
        f"**Source asset:** `{source_urn}`  \n"
        f"**Generator:** {result.generator}  \n"
        f"**Evidence for this asset:** {_asset_evidence(affected_asset)}\n\n"
        "### Proposed SQL\n\n"
        f"```sql\n{result.change_sql}\n```\n\n"
        "### Impact report\n\n"
        f"{result.impact.summary}\n\n"
        "_BlastRadius generated this as a review note. It did not execute the "
        "schema change._"
    )


def write_impact_notes(
    result: Any,
    source_urn: str,
    *,
    graph: Any | None = None,
    timestamp_ms: int | None = None,
) -> tuple[str, ...]:
    """Upsert one URL-keyed institutional-memory note per downstream asset."""
    if not result.impact.downstream:
        return ()

    if graph is None:
        load_dotenv()
        graph = DataHubGraph(
            DatahubClientConfig(
                server=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
                token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
                datahub_component="blastradius",
            )
        )

    applied_at = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    written = []
    for asset in result.impact.downstream:
        try:
            memory = graph.get_aspect(asset["urn"], InstitutionalMemoryClass)
            elements = list(memory.elements) if memory else []
            existing = next(
                (element for element in elements if element.url == BLASTRADIUS_NOTE_URL),
                None,
            )
            stamp = AuditStampClass(time=applied_at, actor=BLASTRADIUS_ACTOR_URN)
            note = InstitutionalMemoryMetadataClass(
                url=BLASTRADIUS_NOTE_URL,
                description=impact_note_markdown(result, source_urn, asset),
                createStamp=existing.createStamp if existing else stamp,
                updateStamp=stamp if existing else None,
            )
            elements = [
                element
                for element in elements
                if element.url != BLASTRADIUS_NOTE_URL
            ]
            elements.append(note)
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=asset["urn"],
                    aspect=InstitutionalMemoryClass(elements=elements),
                )
            )
        except Exception as exc:
            raise RuntimeError(
                f"DataHub write-back failed for {asset['urn']}: {exc}"
            ) from exc
        written.append(asset["urn"])

    return tuple(written)
