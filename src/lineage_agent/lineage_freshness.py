"""Read DataHub lineage with the search cache explicitly bypassed."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping


CACHE_BYPASS_METHOD = "DataHub GraphQL searchAcrossLineage skipCache=true"
MAX_LINEAGE_RESULTS = 50

LINEAGE_QUERY = """
query RippleProofUncachedLineage($input: SearchAcrossLineageInput!) {
  searchAcrossLineage(input: $input) {
    total
    searchResults {
      entity {
        urn
        type
      }
      paths {
        path {
          urn
          type
          ... on SchemaFieldEntity {
            fieldPath
            parent {
              urn
              type
            }
          }
        }
      }
      degree
    }
  }
}
"""


@dataclass(frozen=True)
class UncachedLineageObservation:
    """Column and asset lineage returned with DataHub search caching bypassed."""

    method: str
    skip_cache: bool
    column_downstream: tuple[dict[str, Any], ...]
    asset_downstream: tuple[dict[str, Any], ...]


def _lineage_input(urn: str, max_hops: int) -> dict[str, Any]:
    if max_hops < 1:
        raise ValueError("Lineage hop limit must be at least 1")
    degrees = [str(value) for value in range(1, max_hops + 1)]
    if max_hops >= 3:
        degrees = ["1", "2", "3+"]
    return {
        "urn": urn,
        "query": "*",
        "start": 0,
        "count": MAX_LINEAGE_RESULTS,
        "types": None,
        "orFilters": [
            {
                "and": [
                    {
                        "field": "degree",
                        "condition": "EQUAL",
                        "values": degrees,
                    },
                    {
                        "field": "removed",
                        "condition": "EQUAL",
                        "values": ["true"],
                        "negated": True,
                    },
                ]
            }
        ],
        "searchFlags": {
            "skipHighlighting": True,
            "maxAggValues": 3,
            "skipCache": True,
        },
        "direction": "DOWNSTREAM",
    }


def _lineage_results(
    payload: Mapping[str, Any],
    *,
    column_level: bool,
) -> tuple[dict[str, Any], ...]:
    results = payload.get("searchResults")
    total = payload.get("total")
    if not isinstance(results, list) or not isinstance(total, int):
        raise RuntimeError("DataHub cache-bypassed lineage response is incomplete")
    if total > len(results):
        raise RuntimeError(
            f"DataHub cache-bypassed lineage reported {total} result(s) but returned "
            f"only {len(results)}. RippleProof refuses truncated freshness evidence."
        )

    normalized: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, Mapping):
            continue
        entity = result.get("entity") or {}
        urn = entity.get("urn") if isinstance(entity, Mapping) else None
        entity_type = entity.get("type") if isinstance(entity, Mapping) else None
        if not isinstance(urn, str) or not isinstance(entity_type, str):
            continue
        item: dict[str, Any] = {
            "urn": urn,
            "type": entity_type,
            "degree": result.get("degree") if isinstance(result.get("degree"), int) else None,
        }
        if column_level:
            columns: set[str] = set()
            paths = result.get("paths") or ()
            if isinstance(paths, list):
                for path_record in paths:
                    if not isinstance(path_record, Mapping):
                        continue
                    path = path_record.get("path") or ()
                    if not isinstance(path, list) or not path:
                        continue
                    endpoint = path[-1]
                    if not isinstance(endpoint, Mapping):
                        continue
                    field_path = endpoint.get("fieldPath")
                    if endpoint.get("type") == "SCHEMA_FIELD" and isinstance(
                        field_path, str
                    ):
                        columns.add(field_path)
            item["columns"] = tuple(sorted(columns))
        normalized.append(item)
    return tuple(sorted(normalized, key=lambda item: item["urn"]))


def _new_graph() -> Any:
    from datahub.ingestion.graph.client import DataHubGraph
    from datahub.ingestion.graph.config import DatahubClientConfig
    from dotenv import load_dotenv

    load_dotenv()
    return DataHubGraph(
        DatahubClientConfig(
            server=os.environ.get("DATAHUB_GMS_URL", "http://localhost:8080"),
            token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
            datahub_component="ripple-proof-lineage-freshness",
        )
    )


def _close_graph(graph: Any) -> None:
    close = getattr(graph, "close", None)
    if close:
        close()


def _query_lineage(graph: Any, urn: str, max_hops: int) -> Mapping[str, Any]:
    response = graph.execute_graphql(
        LINEAGE_QUERY,
        variables={"input": _lineage_input(urn, max_hops)},
        operation_name="RippleProofUncachedLineage",
    )
    lineage = response.get("searchAcrossLineage") if isinstance(response, Mapping) else None
    if not isinstance(lineage, Mapping):
        raise RuntimeError("DataHub cache-bypassed lineage query returned no result")
    return lineage


def read_cache_bypassed_lineage_sync(
    asset_urn: str,
    source_column: str,
    max_hops: int,
    *,
    graph_factory: Callable[[], Any] | None = None,
) -> UncachedLineageObservation:
    """Read column and asset lineage directly with search cache bypassed."""
    from datahub.metadata.urns import SchemaFieldUrn

    graph = (graph_factory or _new_graph)()
    try:
        column_urn = str(SchemaFieldUrn(asset_urn, source_column))
        column_payload = _query_lineage(graph, column_urn, max_hops)
        asset_payload = _query_lineage(graph, asset_urn, max_hops)
        return UncachedLineageObservation(
            method=CACHE_BYPASS_METHOD,
            skip_cache=True,
            column_downstream=_lineage_results(column_payload, column_level=True),
            asset_downstream=_lineage_results(asset_payload, column_level=False),
        )
    finally:
        _close_graph(graph)


async def read_cache_bypassed_lineage(
    asset_urn: str,
    source_column: str,
    max_hops: int,
) -> UncachedLineageObservation:
    """Run the synchronous DataHub SDK query without blocking the MCP event loop."""
    return await asyncio.to_thread(
        read_cache_bypassed_lineage_sync,
        asset_urn,
        source_column,
        max_hops,
    )
