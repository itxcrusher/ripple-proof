import unittest

from lineage_agent.lineage_freshness import (
    CACHE_BYPASS_METHOD,
    _lineage_input,
    _lineage_results,
    read_cache_bypassed_lineage_sync,
)


SOURCE_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)"
DOWNSTREAM_URN = "urn:li:dataset:(urn:li:dataPlatform:dbt,orders_daily,PROD)"


def response(*, column: bool):
    path = (
        [
            {
                "path": [
                    {
                        "urn": "urn:li:schemaField:(source,user_id)",
                        "type": "SCHEMA_FIELD",
                        "fieldPath": "user_id",
                    },
                    {
                        "urn": "urn:li:schemaField:(downstream,user_id)",
                        "type": "SCHEMA_FIELD",
                        "fieldPath": "user_id",
                    },
                ]
            }
        ]
        if column
        else []
    )
    return {
        "searchAcrossLineage": {
            "total": 1,
            "searchResults": [
                {
                    "entity": {"urn": DOWNSTREAM_URN, "type": "DATASET"},
                    "degree": 1,
                    "paths": path,
                }
            ],
        }
    }


class FakeGraph:
    def __init__(self):
        self.calls = []
        self.closed = False

    def execute_graphql(self, query, *, variables, operation_name):
        self.calls.append((query, variables, operation_name))
        return response(column=len(self.calls) == 1)

    def close(self):
        self.closed = True


class LineageFreshnessTests(unittest.TestCase):
    def test_query_input_explicitly_bypasses_cache(self):
        value = _lineage_input(SOURCE_URN, 2)

        self.assertIs(value["searchFlags"]["skipCache"], True)
        self.assertEqual(
            value["orFilters"][0]["and"][0]["values"],
            ["1", "2"],
        )

    def test_column_result_uses_path_endpoint(self):
        value = _lineage_results(
            response(column=True)["searchAcrossLineage"],
            column_level=True,
        )

        self.assertEqual(value[0]["urn"], DOWNSTREAM_URN)
        self.assertEqual(value[0]["columns"], ("user_id",))

    def test_truncated_cache_bypassed_result_fails_closed(self):
        payload = response(column=False)["searchAcrossLineage"]
        payload["total"] = 2

        with self.assertRaisesRegex(RuntimeError, "truncated freshness evidence"):
            _lineage_results(payload, column_level=False)

    def test_reads_both_lineage_levels_with_skip_cache_and_closes_client(self):
        graph = FakeGraph()

        observation = read_cache_bypassed_lineage_sync(
            SOURCE_URN,
            "user_id",
            2,
            graph_factory=lambda: graph,
        )

        self.assertEqual(observation.method, CACHE_BYPASS_METHOD)
        self.assertTrue(observation.skip_cache)
        self.assertEqual(len(observation.column_downstream), 1)
        self.assertEqual(len(observation.asset_downstream), 1)
        self.assertEqual(len(graph.calls), 2)
        self.assertTrue(
            all(call[1]["input"]["searchFlags"]["skipCache"] for call in graph.calls)
        )
        self.assertTrue(graph.closed)


if __name__ == "__main__":
    unittest.main()
