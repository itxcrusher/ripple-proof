"""Read-only readiness checks for RippleProof's live DataHub operator path."""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .main import DEFAULT_ASSET_URN, _tool_json
from .mutation_safety import configured_datahub_url, is_loopback_datahub_url


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    status: str
    detail: str


def _bounded_output(completed: subprocess.CompletedProcess[str]) -> str:
    combined = "\n".join(
        value.strip()
        for value in (completed.stdout or "", completed.stderr or "")
        if value and value.strip()
    )
    if not combined:
        return ""
    return " | ".join(combined.splitlines()[-4:])[-800:]


def _run(
    command: Sequence[str],
    *,
    timeout: int = 45,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def check_configuration() -> ReadinessCheck:
    load_dotenv()
    endpoint = configured_datahub_url()
    if not os.environ.get("DATAHUB_GMS_TOKEN", "").strip():
        return ReadinessCheck(
            "DataHub configuration",
            "failed",
            "DATAHUB_GMS_TOKEN is missing. Run datahub init, then copy only the token into .env.",
        )
    locality = "local quickstart" if is_loopback_datahub_url(endpoint) else "remote endpoint"
    return ReadinessCheck(
        "DataHub configuration",
        "passed",
        f"Endpoint and token are configured ({locality}); secret values were not printed.",
    )


def check_docker(
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> ReadinessCheck:
    executable = shutil.which("docker")
    if executable is None:
        return ReadinessCheck(
            "Docker engine",
            "failed",
            "Docker CLI was not found. Install or start Docker Desktop, then retry.",
        )
    try:
        completed = runner(
            [executable, "version", "--format", "{{.Server.Version}}"],
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return ReadinessCheck(
            "Docker engine",
            "failed",
            "Docker did not respond within 20 seconds. Open Docker Desktop and wait for the engine.",
        )
    detail = _bounded_output(completed)
    if completed.returncode != 0:
        return ReadinessCheck(
            "Docker engine",
            "failed",
            detail or "Docker Desktop is installed but its engine is not reachable.",
        )
    return ReadinessCheck("Docker engine", "passed", f"Docker server {detail or 'responded'}.")


def check_local_datahub(
    endpoint: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> ReadinessCheck:
    if not is_loopback_datahub_url(endpoint):
        return ReadinessCheck(
            "DataHub quickstart",
            "skipped",
            "Configured endpoint is remote; the local quickstart stack is not required.",
        )
    executable = shutil.which("datahub")
    if executable is None:
        return ReadinessCheck(
            "DataHub quickstart",
            "failed",
            "DataHub CLI was not found. Install RippleProof with the live extra, then retry.",
        )
    try:
        completed = runner([executable, "docker", "check"], timeout=60)
    except subprocess.TimeoutExpired:
        return ReadinessCheck(
            "DataHub quickstart",
            "failed",
            "datahub docker check timed out. Restart Docker Desktop, then run datahub docker check directly.",
        )
    detail = _bounded_output(completed)
    if completed.returncode != 0:
        guidance = (
            "datahub docker check failed without diagnostics. Open Docker Desktop, wait for the "
            "engine, then run datahub docker check; if no stack exists, run datahub docker quickstart."
            if not detail
            else f"{detail}. Run datahub docker check directly; use datahub docker quickstart if the stack is missing."
        )
        return ReadinessCheck("DataHub quickstart", "failed", guidance)
    return ReadinessCheck(
        "DataHub quickstart",
        "passed",
        detail or "DataHub CLI reported no quickstart issues.",
    )


def _mcp_environment(endpoint: str) -> dict[str, str]:
    allowed = {
        "APPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PYTHONPATH",
        "PYTHONUTF8",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WINDIR",
    }
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in allowed
    }
    environment["DATAHUB_GMS_URL"] = endpoint
    environment["DATAHUB_GMS_TOKEN"] = os.environ.get("DATAHUB_GMS_TOKEN", "")
    environment["LOGURU_LEVEL"] = "WARNING"
    return environment


async def check_datahub_mcp(endpoint: str, asset_urn: str) -> ReadinessCheck:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server_datahub"],
        env=_mcp_environment(endpoint),
    )
    try:
        with open(os.devnull, "w", encoding="utf-8") as mcp_log:
            async with stdio_client(params, errlog=mcp_log) as (read, write):
                async with ClientSession(read, write) as session:
                    await asyncio.wait_for(session.initialize(), timeout=20)
                    tools = await asyncio.wait_for(session.list_tools(), timeout=20)
                    names = {tool.name for tool in tools.tools}
                    required = {"get_lineage", "get_entities", "list_schema_fields"}
                    missing = sorted(required - names)
                    if missing:
                        return ReadinessCheck(
                            "DataHub MCP",
                            "failed",
                            f"Server started but required tools are missing: {', '.join(missing)}.",
                        )
                    result = await asyncio.wait_for(
                        session.call_tool(
                            "get_lineage",
                            {
                                "urn": asset_urn,
                                "column": "user_id",
                                "upstream": False,
                                "max_hops": 2,
                                "max_results": 50,
                            },
                        ),
                        timeout=30,
                    )
                    _tool_json(result, "get_lineage")
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        token = os.environ.get("DATAHUB_GMS_TOKEN", "")
        if token:
            message = message.replace(token, "[REDACTED]")
        return ReadinessCheck(
            "DataHub MCP",
            "failed",
            f"Live MCP read failed: {message[:500]}",
        )
    return ReadinessCheck(
        "DataHub MCP",
        "passed",
        f"Required tools loaded and a live lineage read completed for {asset_urn}.",
    )


def render_results(results: Sequence[ReadinessCheck]) -> str:
    failed = any(result.status == "failed" for result in results)
    rows = [f"LIVE READINESS: {'FAILED' if failed else 'PASSED'}"]
    for result in results:
        rows.extend(
            (
                f"[{result.status.upper()}] {result.name}",
                f"       {result.detail}",
            )
        )
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run read-only checks for Docker, DataHub quickstart, and DataHub MCP."
    )
    parser.add_argument("--asset-urn", default=DEFAULT_ASSET_URN)
    args = parser.parse_args(argv)

    configuration = check_configuration()
    results = [configuration, check_docker()]
    if configuration.status == "passed":
        endpoint = configured_datahub_url()
        results.append(check_local_datahub(endpoint))
        results.append(asyncio.run(check_datahub_mcp(endpoint, args.asset_urn)))
    print(render_results(results))
    if any(result.status == "failed" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
