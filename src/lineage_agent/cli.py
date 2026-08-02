"""Single command-line entry point for RippleProof workflows."""

from __future__ import annotations

import argparse
import importlib
import sys
import webbrowser
from pathlib import Path
from typing import Callable, Sequence


COMMANDS = {
    "plan": ("lineage_agent.main", "Plan a DataHub-grounded schema change campaign."),
    "audit": ("lineage_agent.audit", "Verify a captured campaign offline."),
    "review": ("lineage_agent.replay", "Render a self-contained campaign review page."),
    "evaluate": ("lineage_agent.evaluation", "Run the adversarial safety corpus."),
    "validate-dbt": (
        "lineage_agent.dbt_validation",
        "Execute generated dbt repairs in isolated DuckDB workspaces.",
    ),
    "validate-postgres": (
        "lineage_agent.postgres_validation",
        "Apply and roll back the source migration in disposable PostgreSQL.",
    ),
    "verify-context": (
        "lineage_agent.context_guard",
        "Compare repeated MCP context with cache-bypassed DataHub lineage.",
    ),
    "prove-context-guard": (
        "lineage_agent.context_regression",
        "Exercise and restore owner and lineage drift in the local demo graph.",
    ),
    "publish": (
        "lineage_agent.github_publication",
        "Prepare or publish review-only GitHub pull requests.",
    ),
    "record-decision": (
        "lineage_agent.decision",
        "Record an approved-for-review decision in DataHub.",
    ),
}

COMMAND_EXTRAS = {
    "plan": "live",
    "evaluate": "validation",
    "validate-dbt": "validation",
    "verify-context": "live",
    "prove-context-guard": "live",
    "publish": "live",
    "record-decision": "live",
}


def _help() -> str:
    width = max(len(command) for command in (*COMMANDS, "demo"))
    rows = [
        "RippleProof - make schema changes reviewable before they ripple downstream.",
        "",
        "Usage: ripple-proof <command> [options]",
        "",
        "Quick start: ripple-proof demo --open",
        "",
        "Commands:",
        f"  {'demo'.ljust(width)}  Audit the checked campaign and locate its review page.",
    ]
    rows.extend(
        f"  {command.ljust(width)}  {description}"
        for command, (_, description) in COMMANDS.items()
    )
    rows.extend(
        [
            "",
            "Run 'ripple-proof <command> --help' for command-specific options.",
        ]
    )
    return "\n".join(rows)


def _default_demo_bundle() -> Path:
    relative = Path("examples/generated-ripple-proof-resolved")
    candidates = (Path.cwd() / relative, Path(__file__).resolve().parents[2] / relative)
    for candidate in candidates:
        if (candidate / "manifest.json").is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _demo(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser(
        prog="ripple-proof demo",
        description=(
            "Audit the checked end-to-end campaign and locate its self-contained review page."
        ),
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="campaign bundle (defaults to the checked richer campaign)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the review page in the default browser after a passing audit",
    )
    args = parser.parse_args(list(argv))
    bundle = (args.bundle or _default_demo_bundle()).expanduser().resolve()
    if not (bundle / "manifest.json").is_file():
        parser.error(
            f"campaign bundle not found: {bundle}. Run from the repository or pass --bundle."
        )

    from .audit import audit_campaign, render_audit_text

    audit = audit_campaign(bundle)
    print(render_audit_text(audit))
    review = bundle / "campaign.html"
    if not review.is_file():
        parser.error(f"campaign review page not found: {review}")
    print(f"\nReview page: {review}")
    if audit.status != "passed":
        raise SystemExit(1)
    if args.open:
        if not webbrowser.open(review.as_uri()):
            parser.error(f"could not open the browser; open this file manually: {review}")


def _dispatch(command_name: str, module_name: str, argv: Sequence[str]) -> None:
    try:
        module = importlib.import_module(module_name)
        command: Callable[[list[str] | None], None] = module.main
        command(list(argv))
    except ModuleNotFoundError as error:
        extra = COMMAND_EXTRAS.get(command_name)
        if extra is None or (error.name or "").startswith("lineage_agent"):
            raise
        print(
            f"The '{command_name}' command needs the optional '{extra}' dependencies.\n"
            f"Install them with: python -m pip install -e \".[{extra}]\"",
            file=sys.stderr,
        )
        raise SystemExit(2) from error


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_help())
        return
    command, command_args = args[0], args[1:]
    if command == "demo":
        _demo(command_args)
        return
    target = COMMANDS.get(command)
    if target is None:
        print(f"Unknown command: {command}\n\n{_help()}", file=sys.stderr)
        raise SystemExit(2)
    _dispatch(command, target[0], command_args)


if __name__ == "__main__":
    main()
