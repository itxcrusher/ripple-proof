"""Standalone connectivity test: spawn mcp-server-datahub over stdio, list its
tools, and call get_lineage for the demo scenario. Not part of the agent
itself; a throwaway check that the MCP layer (not just the raw SDK) works.
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv

from lineage_agent.main import DEFAULT_ASSET_URN, _configure_output, _tool_json


async def main() -> None:
    _configure_output()
    load_dotenv()
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_datahub"],
        env={**os.environ, "LOGURU_LEVEL": os.environ.get("LOGURU_LEVEL", "WARNING")},
    )
    with open(os.devnull, "w", encoding="utf-8") as mcp_log:
        async with stdio_client(params, errlog=mcp_log) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"{len(tools.tools)} tools exposed by mcp-server-datahub:")
                for tool in tools.tools:
                    print(f"  - {tool.name}")
                    if tool.name == "get_lineage":
                        print("    schema:", json.dumps(tool.inputSchema, indent=2)[:2000])

                print("\nCalling column-level get_lineage for analytics.orders.user_id...")
                result = await session.call_tool(
                    "get_lineage",
                    {
                        "urn": DEFAULT_ASSET_URN,
                        "column": "user_id",
                        "upstream": False,
                        "max_hops": 2,
                        "max_results": 50,
                    },
                )
                payload = _tool_json(result, "get_lineage")
                hits = payload.get("downstreams", {}).get("searchResults", [])
                print(json.dumps({"downstreamCount": len(hits), "results": hits}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
