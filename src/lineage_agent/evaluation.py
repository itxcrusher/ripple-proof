"""Run a deterministic adversarial evaluation of RippleProof repair planning."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .campaign import plan_repair_campaign
from .contracts import plan_contract_patches


PROJECT_LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
EXPECTED_DECISIONS = {"automate", "manual-review"}


@dataclass(frozen=True)
class EvaluationCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    title: str
    category: str
    expected_decision: str
    actual_decision: str
    patch_count: int
    contract_patch_count: int
    protected_count: int
    unresolved_reasons: tuple[str, ...]
    checks: tuple[EvaluationCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "title": self.title,
            "category": self.category,
            "expectedDecision": self.expected_decision,
            "actualDecision": self.actual_decision,
            "passed": self.passed,
            "patchCount": self.patch_count,
            "contractPatchCount": self.contract_patch_count,
            "protectedCount": self.protected_count,
            "unresolvedReasons": list(self.unresolved_reasons),
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True)
class EvaluationReport:
    corpus_sha256: str
    results: tuple[EvaluationResult, ...]

    @property
    def metrics(self) -> dict[str, Any]:
        automate = [r for r in self.results if r.expected_decision == "automate"]
        manual = [r for r in self.results if r.expected_decision == "manual-review"]
        automated = [r for r in automate if r.actual_decision == "automate"]
        correctly_manual = [
            r for r in manual if r.actual_decision == "manual-review"
        ]
        false_repairs = [r for r in manual if r.actual_decision == "automate"]
        passed = [r for r in self.results if r.passed]

        def ratio(numerator: int, denominator: int) -> float:
            return round(numerator / denominator, 4) if denominator else 0.0

        return {
            "totalCases": len(self.results),
            "automationExpected": len(automate),
            "manualReviewExpected": len(manual),
            "passedCases": len(passed),
            "casePassRate": ratio(len(passed), len(self.results)),
            "repairCoverage": ratio(len(automated), len(automate)),
            "manualReviewRecall": ratio(len(correctly_manual), len(manual)),
            "falseRepairs": len(false_repairs),
            "falseRepairRate": ratio(len(false_repairs), len(manual)),
        }

    @property
    def passed(self) -> bool:
        metrics = self.metrics
        return (
            metrics["casePassRate"] == 1.0
            and metrics["falseRepairRate"] == 0.0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "corpusSha256": self.corpus_sha256,
            "passed": self.passed,
            "metrics": self.metrics,
            "results": [result.to_dict() for result in self.results],
        }


def _json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    if not path.is_file():
        raise ValueError(f"Evaluation corpus does not exist: {path}")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Evaluation corpus is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("Evaluation corpus must contain a JSON object")
    return value, raw


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Evaluation case must provide {label}")
    return value.strip()


def _safe_relative(root: Path, value: Any, label: str) -> Path:
    relative = Path(_required_string(value, label))
    if relative.is_absolute():
        raise ValueError(f"Evaluation {label} must be relative: {relative}")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"Evaluation {label} escapes its repository: {relative}") from None
    return candidate


def _materialize_repositories(
    case: Mapping[str, Any],
    root: Path,
) -> tuple[tuple[Path, ...], dict[Path, str]]:
    repositories = case.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("Evaluation case must provide at least one repository")
    roots: list[Path] = []
    original_files: dict[Path, str] = {}
    labels: set[str] = set()
    for repository in repositories:
        if not isinstance(repository, dict):
            raise ValueError("Evaluation repository must contain an object")
        name = _required_string(repository.get("name"), "repository name")
        if not PROJECT_LABEL_RE.fullmatch(name) or name.casefold() in labels:
            raise ValueError(f"Invalid or duplicate evaluation repository name: {name}")
        labels.add(name.casefold())
        repo_root = (root / name).resolve()
        repo_root.mkdir(parents=True)
        roots.append(repo_root)
        models = repository.get("models")
        if not isinstance(models, list):
            raise ValueError(f"Evaluation repository {name} must provide models")
        manifest_nodes: dict[str, Any] = {}
        for index, model in enumerate(models):
            if not isinstance(model, dict):
                raise ValueError("Evaluation model must contain an object")
            path = _safe_relative(repo_root, model.get("path"), "model path")
            try:
                path.relative_to((repo_root / "models").resolve())
            except ValueError:
                raise ValueError(
                    f"Evaluation model must live under models/: {path}"
                ) from None
            if path.suffix.casefold() != ".sql":
                raise ValueError(f"Evaluation model must be SQL: {path}")
            sql = _required_string(model.get("sql"), "model SQL") + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sql, encoding="utf-8")
            original_files[path] = sql
            model_name = _required_string(model.get("name"), "model name")
            alias = _required_string(model.get("alias", model_name), "model alias")
            manifest_nodes[f"model.{name}.{model_name}.{index}"] = {
                "resource_type": "model",
                "name": model_name,
                "alias": alias,
                "original_file_path": path.relative_to(repo_root).as_posix(),
                "relation_name": f'"analytics"."{alias}"',
            }
        files = repository.get("files") or []
        if not isinstance(files, list):
            raise ValueError(f"Evaluation repository {name} files must contain a list")
        for file in files:
            if not isinstance(file, dict):
                raise ValueError("Evaluation file must contain an object")
            path = _safe_relative(repo_root, file.get("path"), "file path")
            content = _required_string(file.get("content"), "file content") + "\n"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            original_files[path] = content
        target = repo_root / "target"
        target.mkdir()
        manifest_path = target / "manifest.json"
        manifest_content = json.dumps({"nodes": manifest_nodes}, sort_keys=True)
        manifest_path.write_text(manifest_content, encoding="utf-8")
        original_files[manifest_path] = manifest_content
    return tuple(roots), original_files


def _repositories_unchanged(
    repo_roots: Sequence[Path], original_files: Mapping[Path, str]
) -> bool:
    current_files = {
        path
        for repo_root in repo_roots
        for path in repo_root.rglob("*")
        if path.is_file()
    }
    return current_files == set(original_files) and all(
        path.read_text(encoding="utf-8") == content
        for path, content in original_files.items()
    )


def _count_check(name: str, actual: int, assertions: Mapping[str, Any], key: str) -> EvaluationCheck | None:
    expected = assertions.get(key)
    if expected is None:
        return None
    if not isinstance(expected, int) or expected < 0:
        raise ValueError(f"Evaluation assertion {key} must be a non-negative integer")
    return EvaluationCheck(name, actual == expected, f"expected {expected}, observed {actual}")


def _fragment_checks(
    assertions: Mapping[str, Any],
    key: str,
    content: str,
    *,
    should_exist: bool,
) -> list[EvaluationCheck]:
    fragments = assertions.get(key) or []
    if not isinstance(fragments, list) or not all(
        isinstance(fragment, str) and fragment for fragment in fragments
    ):
        raise ValueError(f"Evaluation assertion {key} must contain strings")
    checks = []
    for fragment in fragments:
        present = fragment in content
        checks.append(
            EvaluationCheck(
                f"{key}: {fragment}",
                present if should_exist else not present,
                "fragment present" if present else "fragment absent",
            )
        )
    return checks


def _evaluate_case(case: Mapping[str, Any], root: Path) -> EvaluationResult:
    case_id = _required_string(case.get("id"), "id")
    title = _required_string(case.get("title"), "title")
    category = _required_string(case.get("category"), "category")
    expected = _required_string(case.get("expectedDecision"), "expected decision")
    if expected not in EXPECTED_DECISIONS:
        raise ValueError(f"Unsupported expected decision for {case_id}: {expected}")
    mode = _required_string(case.get("mode"), "downstream mode")
    if mode not in {"preserve", "propagate"}:
        raise ValueError(f"Unsupported downstream mode for {case_id}: {mode}")
    old_column = _required_string(case.get("oldColumn", "user_id"), "old column")
    new_column = _required_string(
        case.get("newColumn", "customer_id"), "new column"
    )
    assets = case.get("assets")
    if not isinstance(assets, list) or not assets or not all(
        isinstance(asset, dict) for asset in assets
    ):
        raise ValueError(f"Evaluation case {case_id} must provide asset objects")

    repo_roots, original_files = _materialize_repositories(case, root)
    projects, patches, protected, unresolved = plan_repair_campaign(
        repo_roots,
        assets,
        old_column,
        new_column,
        mode,
    )
    contract_patches, contract_unresolved = plan_contract_patches(
        {project.name: project.root for project in projects},
        patches,
        old_column,
        new_column,
        mode,
    )
    all_unresolved = (*unresolved, *contract_unresolved)
    actual = "manual-review" if all_unresolved else "automate"
    checks = [
        EvaluationCheck(
            "expected decision",
            actual == expected,
            f"expected {expected}, observed {actual}",
        ),
        EvaluationCheck(
            "source repositories unchanged",
            _repositories_unchanged(repo_roots, original_files),
            f"verified {len(original_files)} source file(s)",
        ),
    ]
    assertions = case.get("assertions") or {}
    if not isinstance(assertions, dict):
        raise ValueError(f"Evaluation case {case_id} assertions must contain an object")
    count_values = (
        ("patch count", len(patches), "patchCount"),
        ("contract patch count", len(contract_patches), "contractPatchCount"),
        ("protected count", len(protected), "protectedCount"),
        ("unresolved count", len(all_unresolved), "unresolvedCount"),
        (
            "preserved output aliases",
            sum(patch.preserved_output_aliases for patch in patches),
            "preservedOutputAliases",
        ),
    )
    for name, actual_count, key in count_values:
        check = _count_check(name, actual_count, assertions, key)
        if check:
            checks.append(check)

    proposed_models = "\n".join(patch.proposed_sql for patch in patches)
    proposed_contracts = "\n".join(
        patch.proposed_yaml for patch in contract_patches
    )
    reasons = tuple(item.reason.replace("\\", "/") for item in all_unresolved)
    checks.extend(
        _fragment_checks(
            assertions, "modelContains", proposed_models, should_exist=True
        )
    )
    checks.extend(
        _fragment_checks(
            assertions, "modelExcludes", proposed_models, should_exist=False
        )
    )
    checks.extend(
        _fragment_checks(
            assertions, "contractContains", proposed_contracts, should_exist=True
        )
    )
    reason_fragments = assertions.get("reasonContains") or []
    if not isinstance(reason_fragments, list) or not all(
        isinstance(fragment, str) and fragment for fragment in reason_fragments
    ):
        raise ValueError("Evaluation assertion reasonContains must contain strings")
    reason_text = "\n".join(reasons).casefold()
    for fragment in reason_fragments:
        checks.append(
            EvaluationCheck(
                f"reason contains: {fragment}",
                fragment.casefold() in reason_text,
                "matched an unresolved reason",
            )
        )
    discovery = assertions.get("discovery")
    if discovery is not None:
        discovery_value = _required_string(discovery, "discovery assertion")
        checks.append(
            EvaluationCheck(
                "model discovery",
                bool(patches)
                and all(patch.discovery == discovery_value for patch in patches),
                ", ".join(patch.discovery for patch in patches) or "no patches",
            )
        )

    return EvaluationResult(
        case_id=case_id,
        title=title,
        category=category,
        expected_decision=expected,
        actual_decision=actual,
        patch_count=len(patches),
        contract_patch_count=len(contract_patches),
        protected_count=len(protected),
        unresolved_reasons=reasons,
        checks=tuple(checks),
    )


def run_evaluation(corpus_path: str | Path) -> EvaluationReport:
    path = Path(corpus_path).expanduser().resolve()
    corpus, raw = _json_object(path)
    cases = corpus.get("cases")
    if corpus.get("schemaVersion") != 1 or not isinstance(cases, list) or not cases:
        raise ValueError("Evaluation corpus must use schemaVersion 1 and contain cases")
    ids: set[str] = set()
    results = []
    with tempfile.TemporaryDirectory(prefix="ripple-proof-evaluation-") as directory:
        root = Path(directory)
        for index, case in enumerate(cases):
            if not isinstance(case, dict):
                raise ValueError("Evaluation corpus cases must contain objects")
            case_id = _required_string(case.get("id"), "id")
            if case_id in ids:
                raise ValueError(f"Duplicate evaluation case id: {case_id}")
            ids.add(case_id)
            results.append(_evaluate_case(case, root / f"{index:02d}-{case_id}"))
    return EvaluationReport(
        corpus_sha256=hashlib.sha256(raw).hexdigest(),
        results=tuple(results),
    )


def render_report_html(report: EvaluationReport) -> str:
    metrics = report.metrics
    cards = (
        ("Cases passed", f"{metrics['passedCases']}/{metrics['totalCases']}", "All declared semantic checks"),
        ("Repair coverage", f"{metrics['repairCoverage'] * 100:.0f}%", f"{metrics['automationExpected']} expected automation cases"),
        ("Manual-review recall", f"{metrics['manualReviewRecall'] * 100:.0f}%", f"{metrics['manualReviewExpected']} unsafe or uncertain cases"),
        ("False repairs", str(metrics["falseRepairs"]), f"{metrics['falseRepairRate'] * 100:.0f}% of negative cases"),
    )
    card_html = "".join(
        f'<article class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong><small>{html.escape(detail)}</small></article>'
        for label, value, detail in cards
    )
    rows = []
    for result in report.results:
        reasons = " ".join(result.unresolved_reasons) or "No unresolved condition."
        failed_checks = [check.name for check in result.checks if not check.passed]
        check_summary = (
            "All semantic checks passed."
            if not failed_checks
            else "Failed: " + ", ".join(failed_checks)
        )
        rows.append(
            "<tr>"
            f'<td><strong>{html.escape(result.title)}</strong><small>{html.escape(result.case_id)} · {html.escape(result.category)}</small></td>'
            f'<td><span class="decision">{html.escape(result.expected_decision)}</span></td>'
            f'<td><span class="decision">{html.escape(result.actual_decision)}</span></td>'
            f'<td>{result.patch_count} SQL / {result.contract_patch_count} contract / {result.protected_count} protected</td>'
            f'<td><span class="{"pass" if result.passed else "fail"}">{"PASS" if result.passed else "FAIL"}</span><small>{html.escape(check_summary)} {html.escape(reasons)}</small></td>'
            "</tr>"
        )
    status = "PASSED" if report.passed else "FAILED"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="data:,">
  <title>RippleProof adversarial evaluation</title>
  <style>
    :root {{ --ink:#14253a; --soft:#41566e; --paper:#f5f7f6; --surface:#fcfdfc; --line:#cbd6dc; --primary:#075f96; --success:#176b4d; --success-soft:#dcefe7; --danger:#a3312b; --danger-soft:#f6dfdc; --mono:"Cascadia Code",Consolas,monospace; --sans:"Segoe UI",system-ui,sans-serif; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-width:320px; background:var(--paper); color:var(--ink); font:16px/1.5 var(--sans); }}
    main {{ width:min(1200px,calc(100% - 40px)); margin:0 auto; padding:40px 0 64px; }}
    header {{ padding:28px; border:1px solid var(--line); border-radius:12px; background:var(--surface); }}
    .eyebrow {{ color:var(--success); font:750 12px/1.2 var(--mono); letter-spacing:.06em; }}
    h1 {{ margin:10px 0 8px; font-size:clamp(30px,5vw,54px); line-height:1.02; letter-spacing:-.04em; }}
    .lede {{ max-width:72ch; margin:0; color:var(--soft); font-size:17px; }}
    .hash {{ margin-top:18px; overflow-wrap:anywhere; color:var(--soft); font:12px/1.5 var(--mono); }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:14px 0; }}
    .metric {{ padding:16px 18px; border:1px solid var(--line); border-radius:10px; background:var(--surface); }}
    .metric span,.metric small {{ display:block; color:var(--soft); }}
    .metric strong {{ display:block; margin:4px 0; font:750 28px/1.1 var(--mono); }}
    .metric small {{ font-size:12px; }}
    .table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px; background:var(--surface); }}
    .table-wrap:focus-visible {{ outline:3px solid var(--primary); outline-offset:3px; }}
    table {{ width:100%; border-collapse:collapse; min-width:900px; }}
    th,td {{ padding:14px 16px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); }}
    th {{ color:var(--soft); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    td {{ font-size:13px; }}
    td:first-child {{ width:27%; }}
    td small {{ display:block; margin-top:5px; color:var(--soft); }}
    .decision {{ font:650 12px/1.4 var(--mono); }}
    .pass,.fail {{ display:inline-block; padding:4px 8px; border-radius:5px; font:750 11px/1.2 var(--mono); }}
    .pass {{ background:var(--success-soft); color:var(--success); }}
    .fail {{ background:var(--danger-soft); color:var(--danger); }}
    .scroll-hint {{ display:none; margin:14px 2px 8px; color:var(--soft); font:650 12px/1.4 var(--mono); }}
    .note {{ margin:14px 0 0; color:var(--soft); font-size:13px; }}
    .sr-only {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
    @media (max-width:760px) {{ main {{ width:min(100% - 24px,1200px); padding-top:16px; }} header {{ padding:20px; }} .metrics {{ grid-template-columns:1fr 1fr; gap:8px; }} .metric {{ padding:13px; }} .metric strong {{ font-size:23px; }} }}
    @media (max-width:920px) {{ .scroll-hint {{ display:block; }} }}
    @media (prefers-reduced-motion:reduce) {{ *,*::before,*::after {{ scroll-behavior:auto!important; transition-duration:.01ms!important; animation-duration:.01ms!important; }} }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="eyebrow">{status} · DETERMINISTIC CORPUS</div>
      <h1>Adversarial repair evaluation</h1>
      <p class="lede">RippleProof is measured on both sides of the safety boundary: repairing supported dbt changes and refusing uncertain or unsafe automation. A false repair is any automatic plan produced for a case declared to require human review.</p>
      <div class="hash">Corpus SHA-256: {html.escape(report.corpus_sha256)}</div>
    </header>
    <section class="metrics" aria-label="Evaluation metrics">{card_html}</section>
    <p class="scroll-hint">Scroll the case table horizontally to inspect every evidence column.</p>
    <div class="table-wrap" tabindex="0" aria-label="Adversarial evaluation cases; scroll horizontally to view every column">
      <table>
        <caption class="sr-only">Expected and observed safety decisions for every adversarial evaluation case</caption>
        <thead><tr><th>Case</th><th>Expected</th><th>Observed</th><th>Artifacts</th><th>Evidence</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    <p class="note">The corpus runs locally against the same deterministic campaign-planning functions used by RippleProof. It makes no network requests, executes no migration, and does not modify source repositories.</p>
  </main>
</body>
</html>
"""


def write_report(
    report: EvaluationReport,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
) -> tuple[Path, Path]:
    root = Path(output_dir).expanduser().resolve()
    json_path = root / "report.json"
    html_path = root / "report.html"
    existing = [path for path in (json_path, html_path) if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(f"Evaluation output already exists: {existing[0]}")
    root.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    html_path.write_text(render_report_html(report), encoding="utf-8")
    return json_path, html_path


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the RippleProof adversarial repair corpus."
    )
    parser.add_argument("--corpus", default="evaluation/corpus.json")
    parser.add_argument("--output-dir")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.force and not args.output_dir:
        parser.error("--force requires --output-dir")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = _arguments(argv)
    try:
        report = run_evaluation(args.corpus)
        outputs = (
            write_report(report, args.output_dir, overwrite=args.force)
            if args.output_dir
            else None
        )
    except (FileExistsError, ValueError) as exc:
        print(f"RippleProof evaluation failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    metrics = report.metrics
    print(f"Adversarial evaluation: {'PASSED' if report.passed else 'FAILED'}")
    print(f"  Cases: {metrics['passedCases']}/{metrics['totalCases']}")
    print(f"  Repair coverage: {metrics['repairCoverage'] * 100:.0f}%")
    print(f"  Manual-review recall: {metrics['manualReviewRecall'] * 100:.0f}%")
    print(
        f"  False repairs: {metrics['falseRepairs']} "
        f"({metrics['falseRepairRate'] * 100:.0f}%)"
    )
    if outputs:
        print(f"  JSON evidence: {outputs[0]}")
        print(f"  HTML evidence: {outputs[1]}")
    if not report.passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
