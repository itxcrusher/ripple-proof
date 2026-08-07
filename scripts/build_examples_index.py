"""Generate examples/index.html, the gallery of captured runs.

Every number, asset name and disposition on that page is read out of the
example's own manifest. Nothing about a run is typed by hand, so the gallery
cannot drift away from the evidence it is describing. Re-run this script after
regenerating any example; `tests/test_examples.py` fails if the committed page
no longer matches what this script produces.

    PYTHONPATH=src python scripts/build_examples_index.py
"""

from __future__ import annotations

import html
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"
OUTPUT = EXAMPLES / "index.html"

# Disposition -> the colour it carries everywhere on the site.
DISPOSITION_TONE = {
    "patched": "ok",
    "protected": "held",
    "manual-review": "hold",
    "source-change": "src",
}

# Which captured runs the gallery presents, in order, and the one sentence that
# says what makes each one worth opening. One bundle on disk is deliberately
# absent: `generated-rich-propagate-resolved` is an earlier capture of the same
# scenario as the flagship, recorded before contract asset URNs and expected-node
# assertions existed, so listing it would show the same run twice.
@dataclass(frozen=True)
class Entry:
    directory: str
    title: str
    blurb: str
    featured: bool = False
    walkthrough: str | None = None
    extra_links: tuple[tuple[str, str], ...] = field(default=())


ENTRIES: tuple[Entry, ...] = (
    Entry(
        directory="generated-ripple-proof-resolved",
        title="The full run, end to end",
        blurb=(
            "The complete campaign. A human resolved the ambiguous model, every repair was "
            "built in an isolated sandbox, the migration was applied and rolled back in a "
            "disposable PostgreSQL, and review-only pull requests were opened."
        ),
        featured=True,
        walkthrough="run.html",
        extra_links=(("Generated code", "workspace.html"),),
    ),
    Entry(
        directory="generated-rich-propagate",
        title="When validation fails",
        blurb=(
            "The same five-asset graph, but the dbt build did not pass. The campaign stopped "
            "instead of publishing, and the unproven assets went to manual review."
        ),
    ),
    Entry(
        directory="generated-rich-preserve",
        title="When contracts protect downstream",
        blurb=(
            "In preserve mode the downstream column names stay as they are, so assets covered "
            "by a data contract are protected rather than rewritten."
        ),
        extra_links=(("Compare both modes", "richer-graph-review.html"),),
    ),
    Entry(
        directory="generated-propagate",
        title="A smaller graph, propagated",
        blurb=(
            "A three-asset graph with the rename carried downstream, captured before the "
            "project was renamed and still bound to its manifest under the old namespace."
        ),
    ),
    Entry(
        directory="generated",
        title="The baseline capture",
        blurb=(
            "The smallest run in the corpus: three assets, preserve mode, one repaired project. "
            "Useful as the floor to compare the richer runs against."
        ),
    ),
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _count_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return len([p for p in directory.rglob("*") if p.is_file()])


def read_example(entry: Entry) -> dict:
    """Pull every fact the gallery shows straight out of the captured run."""
    directory = EXAMPLES / entry.directory
    manifest = _load(directory / "manifest.json")

    assignments = manifest.get("reviewAssignments") or []
    dispositions = Counter(a.get("disposition") for a in assignments if a.get("disposition"))

    validation = manifest.get("dbtValidation") or {}
    dbt_status = validation.get("status") if isinstance(validation, dict) else None
    projects = len(validation.get("projects") or []) if isinstance(validation, dict) else 0

    pull_requests = 0
    publication_path = directory / "publication.json"
    if publication_path.exists():
        publication = _load(publication_path)
        for key in ("pullRequests", "pull_requests", "publishedPullRequests"):
            value = publication.get(key)
            if isinstance(value, list):
                pull_requests = len(value)
                break

    # Downstream nodes for the card diagram, in graph order, carrying the real
    # disposition each one received. The source row is drawn separately.
    nodes = [
        {
            "name": a.get("assetName", ""),
            "disposition": a.get("disposition", ""),
            "tone": DISPOSITION_TONE.get(a.get("disposition", ""), "idle"),
        }
        for a in assignments
        if a.get("disposition") != "source-change"
    ]
    if not nodes:
        # Older captures predate reviewAssignments; fall back to the lineage graph
        # and show the assets without claiming a disposition that was not recorded.
        nodes = [
            {"name": a.get("name", ""), "disposition": "", "tone": "idle"}
            for a in (manifest.get("lineageAssets") or [])
            if a.get("platform") != "postgres"
        ]

    return {
        "entry": entry,
        "directory": entry.directory,
        "mode": manifest.get("downstreamColumnMode") or "unknown",
        "assets": len(manifest.get("lineageAssets") or []),
        "dispositions": dispositions,
        "dbt_status": dbt_status,
        "projects": projects,
        "patch_files": _count_files(directory / "patches"),
        "evidence_files": len(sorted(directory.glob("*.json"))),
        "pull_requests": pull_requests,
        "nodes": nodes,
        "manual_review": len(manifest.get("manualReview") or []),
    }


def _stat(value: str, label: str, tone: str = "") -> str:
    cls = f" {tone}" if tone else ""
    return (
        f'<div class="stat"><b class="{cls.strip()}">{html.escape(str(value))}</b>'
        f"<span>{html.escape(label)}</span></div>"
    )


def _diagram(data: dict, index: int) -> str:
    """A compact ripple, drawn from this run's own downstream assets."""
    nodes = data["nodes"][:6]
    if not nodes:
        return ""
    row = 30
    height = max(len(nodes) * row + 16, 104)
    mid = height / 2
    # A deliberately small viewBox so the browser scales the whole drawing up:
    # at a 340px column this renders roughly 1.3x, which is what makes the
    # 8.5px labels legible instead of the hairline text of the first attempt.
    parts = [
        f'<svg class="mini" viewBox="0 0 280 {height:.0f}" role="img" '
        f'aria-label="{html.escape(_diagram_label(data))}">'
    ]
    for i, node in enumerate(nodes):
        y = 12 + i * row + 11
        parts.append(
            f'<path class="me me-{node["tone"]}" pathLength="1" '
            f'style="--d:{i * 0.11:.2f}s" d="M100 {mid:.0f} C114 {mid:.0f} 116 {y} 126 {y}"/>'
        )
    parts.append(
        f'<rect class="msrc" x="4" y="{mid - 17:.0f}" width="96" height="34" rx="8"/>'
        f'<text class="mtxt msrc-t" x="12" y="{mid + 4:.0f}">analytics.orders</text>'
    )
    for i, node in enumerate(nodes):
        y = 12 + i * row
        label = node["name"][:24]
        parts.append(
            f'<rect class="mn mn-{node["tone"]}" x="126" y="{y}" width="150" height="22" rx="6" '
            f'style="--d:{i * 0.11:.2f}s"/>'
            f'<text class="mtxt" x="134" y="{y + 14}">{html.escape(label)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _diagram_label(data: dict) -> str:
    counts = ", ".join(f"{n} {d}" for d, n in sorted(data["dispositions"].items())) or "no recorded dispositions"
    return (
        f"Lineage for the {data['directory']} run: analytics.orders fanning out to "
        f"{len(data['nodes'])} downstream assets ({counts})."
    )


def _card(data: dict, index: int) -> str:
    entry = data["entry"]
    directory = data["directory"]
    stats = []
    stats.append(_stat(data["assets"], "assets in graph"))
    if data["dispositions"].get("patched"):
        stats.append(_stat(data["dispositions"]["patched"], "repaired", "ok"))
    if data["dispositions"].get("protected"):
        stats.append(_stat(data["dispositions"]["protected"], "contract-protected", "held"))
    held = data["dispositions"].get("manual-review") or data["manual_review"]
    if held:
        stats.append(_stat(held, "sent to manual review", "hold"))
    if data["dbt_status"]:
        tone = "ok" if data["dbt_status"] == "passed" else "bad"
        plural = "project" if data["projects"] == 1 else "projects"
        stats.append(_stat(data["dbt_status"], f"dbt build, {data['projects']} {plural}", tone))
    if data["pull_requests"]:
        stats.append(_stat(data["pull_requests"], "review-only pull requests"))

    links = [
        f'<a class="btn btn-primary" href="{directory}/campaign.html" target="_blank" '
        f'rel="noopener">Open the evidence{_arrow()}</a>'
    ]
    if entry.walkthrough:
        links.insert(
            0,
            f'<a class="btn btn-primary" href="{entry.walkthrough}" target="_blank" '
            f'rel="noopener">Walk through it{_arrow()}</a>',
        )
        links[1] = links[1].replace("btn-primary", "btn-ghost")
    for label, href in entry.extra_links:
        links.append(
            f'<a class="btn btn-ghost" href="{href}" target="_blank" rel="noopener">'
            f"{html.escape(label)}{_arrow()}</a>"
        )

    mode_note = "downstream names kept" if data["mode"] == "preserve" else "rename carried downstream"
    return f"""      <article class="card{' feat' if entry.featured else ''}">
        <div class="card-body">
          <div class="card-meta">
            <span class="pill pill-{data['mode']}">{html.escape(data['mode'])}</span>
            <span class="meta-note">{html.escape(mode_note)}</span>{'''
            <span class="pill pill-feat">start here</span>''' if entry.featured else ''}
          </div>
          <h2>{html.escape(entry.title)}</h2>
          <p>{html.escape(entry.blurb)}</p>
          <div class="stats">{''.join(stats)}</div>
          <div class="cta">{''.join(links)}</div>
        </div>
        <div class="card-viz">{_diagram(data, index)}</div>
      </article>"""


def _arrow() -> str:
    return (
        '<svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" '
        'stroke-width="1.6" aria-hidden="true"><path d="M3.5 8.5l5-5M4.5 3.5h4v4"/></svg>'
    )


def render() -> str:
    examples = [read_example(entry) for entry in ENTRIES]
    cards = "\n".join(_card(data, i) for i, data in enumerate(examples))
    total_assets = sum(d["assets"] for d in examples)
    total_held = sum((d["dispositions"].get("manual-review") or d["manual_review"]) for d in examples)
    return TEMPLATE.format(
        cards=cards,
        count=len(examples),
        total_assets=total_assets,
        total_held=total_held,
        arrow=_arrow(),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>RippleProof | Captured runs</title>
<meta name="description" content="Every RippleProof run in this repository, kept whole: the lineage it read, the repairs it wrote, the builds it ran, and the cases where it refused.">
<link rel="canonical" href="https://itxcrusher.github.io/ripple-proof/examples/">
<link rel="stylesheet" href="../assets/site.css">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='%234cc38a'/><circle cx='16' cy='16' r='4' fill='%2308090a'/><circle cx='16' cy='16' r='9' fill='none' stroke='%2308090a' stroke-width='2'/></svg>">
<style>
/* page-specific only; the design system is assets/site.css */
.head{{padding:76px 0 44px}}
.kicker{{font-family:var(--mono);font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--ink-3);margin-bottom:14px}}
h1{{font-size:clamp(2rem,4vw,2.9rem);max-width:20ch;text-wrap:balance}}
.head p{{margin-top:18px;color:var(--ink-2);max-width:70ch;font-size:16.5px}}
.head .rollup{{margin-top:26px;display:flex;flex-wrap:wrap;gap:10px 26px;font-size:13.5px;
  color:var(--ink-3);font-family:var(--mono)}}
.head .rollup b{{color:var(--mint);font-variation-settings:"wght" 500}}

.grid{{display:grid;gap:20px;padding-bottom:80px}}
.card{{border:1px solid var(--line);border-radius:14px;background:var(--surface);
  display:grid;overflow:hidden;transition:border-color .2s ease,transform .2s cubic-bezier(.16,1,.3,1)}}
.card:hover{{border-color:var(--line-2);transform:translateY(-2px)}}
@media(min-width:900px){{.card{{grid-template-columns:minmax(0,1fr) 340px;align-items:center}}}}
.card.feat{{background:linear-gradient(180deg,var(--raised),var(--surface));
  border-color:rgba(76,195,138,.26)}}
.card-body{{padding:28px 30px}}
.card-viz{{padding:24px 26px 24px 0;display:flex;justify-content:center;align-items:center}}
.card-viz > *{{width:100%;border:1px solid var(--line);border-radius:11px;padding:12px 10px;
  background:var(--sunk)}}
@media(max-width:899px){{.card-viz{{padding:0 26px 26px}}}}
.card-meta{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:14px}}
.pill{{font-family:var(--mono);font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
  border-radius:20px;padding:3px 10px;border:1px solid var(--line-2);color:var(--ink-2)}}
.pill-propagate{{color:var(--mint);border-color:rgba(76,195,138,.34)}}
.pill-preserve{{color:var(--indigo);border-color:rgba(139,149,232,.34)}}
.pill-feat{{color:#04120b;background:var(--mint);border-color:var(--mint)}}
.meta-note{{font-size:12.5px;color:var(--ink-3)}}
.card h2{{font-size:1.32rem}}
.card p{{margin-top:11px;color:var(--ink-2);font-size:14.8px;max-width:62ch}}
.stats{{display:flex;flex-wrap:wrap;gap:20px 28px;margin:22px 0 24px}}
.stat b{{display:block;font-family:var(--mono);font-size:1.35rem;font-variation-settings:"wght" 500;
  line-height:1;letter-spacing:-.02em;color:var(--ink)}}
.stat b.ok{{color:var(--mint)}} .stat b.hold{{color:var(--amber)}}
.stat b.held{{color:var(--indigo)}} .stat b.bad{{color:var(--red)}}
.stat span{{display:block;margin-top:7px;font-size:12.5px;color:var(--ink-3)}}
.cta{{display:flex;flex-wrap:wrap;gap:10px}}
.btn{{display:inline-flex;align-items:center;gap:7px;padding:9px 16px;border-radius:8px;
  font-size:14px;font-variation-settings:"wght" 540;text-decoration:none;border:1px solid transparent;
  transition:background .18s ease,border-color .18s ease}}
.btn-primary{{background:var(--mint);color:#04120b}}
.btn-primary:hover{{background:#5ed69a}}
.btn-ghost{{border-color:var(--line-2);color:var(--ink)}}
.btn-ghost:hover{{border-color:var(--ink-3);background:rgba(255,255,255,.03)}}
.btn svg{{opacity:.7}}

/* the per-card ripple, same language as the landing page hero */
.mini{{display:block;width:100%;height:auto}}
.msrc{{fill:rgba(76,195,138,.07);stroke:var(--mint-deep);stroke-width:1}}
.mtxt{{font-family:var(--mono);font-size:8.5px;fill:var(--ink-2)}}
.msrc-t{{fill:var(--ink)}}
.me{{fill:none;stroke:#23262b;stroke-width:1.2;stroke-dasharray:1;stroke-dashoffset:0;
  animation:mdraw 6s linear infinite;animation-delay:var(--d)}}
@keyframes mdraw{{
  0%,12%{{stroke-dashoffset:1;stroke:#23262b}}
  26%,88%{{stroke-dashoffset:0;stroke:var(--mint-deep)}}
  97%,100%{{stroke-dashoffset:1;stroke:#23262b}}
}}
.me-hold{{animation-name:mdraw-hold}} .me-held{{animation-name:mdraw-held}}
@keyframes mdraw-hold{{
  0%,12%{{stroke-dashoffset:1;stroke:#23262b}}
  26%,88%{{stroke-dashoffset:0;stroke:var(--amber)}}
  97%,100%{{stroke-dashoffset:1;stroke:#23262b}}
}}
@keyframes mdraw-held{{
  0%,12%{{stroke-dashoffset:1;stroke:#23262b}}
  26%,88%{{stroke-dashoffset:0;stroke:var(--indigo)}}
  97%,100%{{stroke-dashoffset:1;stroke:#23262b}}
}}
.mn{{fill:var(--sunk);stroke:#23262b;stroke-width:1;animation:mlit 6s linear infinite;
  animation-delay:var(--d)}}
@keyframes mlit{{
  0%,18%{{stroke:#23262b;fill:var(--sunk)}}
  30%,88%{{stroke:var(--mint-deep);fill:rgba(76,195,138,.05)}}
  97%,100%{{stroke:#23262b;fill:var(--sunk)}}
}}
.mn-hold{{animation-name:mlit-hold}} .mn-held{{animation-name:mlit-held}} .mn-idle{{animation-name:mlit-idle}}
@keyframes mlit-hold{{
  0%,18%{{stroke:#23262b;fill:var(--sunk)}}
  30%,88%{{stroke:var(--amber);fill:rgba(224,164,88,.07)}}
  97%,100%{{stroke:#23262b;fill:var(--sunk)}}
}}
@keyframes mlit-held{{
  0%,18%{{stroke:#23262b;fill:var(--sunk)}}
  30%,88%{{stroke:var(--indigo);fill:rgba(139,149,232,.06)}}
  97%,100%{{stroke:#23262b;fill:var(--sunk)}}
}}
@keyframes mlit-idle{{
  0%,18%{{stroke:#23262b;fill:var(--sunk)}}
  30%,88%{{stroke:var(--line-2);fill:rgba(255,255,255,.03)}}
  97%,100%{{stroke:#23262b;fill:var(--sunk)}}
}}
@media(prefers-reduced-motion:reduce){{
  .me,.mn{{animation:none!important;stroke-dashoffset:0}}
  .me{{stroke:var(--mint-deep)}} .me-hold{{stroke:var(--amber)}} .me-held{{stroke:var(--indigo)}}
  .mn{{stroke:var(--mint-deep);fill:rgba(76,195,138,.05)}}
  .mn-hold{{stroke:var(--amber);fill:rgba(224,164,88,.07)}}
  .mn-held{{stroke:var(--indigo);fill:rgba(139,149,232,.06)}}
  .card:hover{{transform:none}}
}}

footer{{padding:40px 0 60px;border-top:1px solid var(--line);color:var(--ink-3);font-size:13.5px}}
footer a{{color:var(--ink-2);text-decoration:none}}
footer a:hover{{color:var(--ink);text-decoration:underline}}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>

<header class="nav">
  <div class="wrap">
    <a class="brand" href="../index.html">
      <svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#4cc38a"/><circle cx="16" cy="16" r="3.6" fill="#08090a"/><circle cx="16" cy="16" r="8.4" fill="none" stroke="#08090a" stroke-width="1.9"/></svg>
      RippleProof
    </a>
    <nav aria-label="Primary">
      <a href="../index.html">&larr; Back to the tool</a>
      <a class="gh hide-sm" href="https://github.com/itxcrusher/ripple-proof" target="_blank" rel="noopener">GitHub{arrow}</a>
    </nav>
  </div>
</header>

<main id="main">
  <div class="wrap head">
    <div class="kicker">Captured runs &middot; not your data</div>
    <h1>Every run, kept whole.</h1>
    <p>Each card below is a real RippleProof run against a demo warehouse, recorded in full and committed to this repository. You can read the lineage it was given, the repairs it wrote, the builds it ran, and the exact points where it stopped and refused. They differ on purpose: some propagate the rename downstream, some preserve the downstream names, one of them fails its build.</p>
    <div class="rollup">
      <span><b>{count}</b> captured runs</span>
      <span><b>{total_assets}</b> downstream assets across them</span>
      <span><b>{total_held}</b> held back for a human</span>
    </div>
    <p class="note" style="margin-top:22px">The same graph behaves differently depending on whether the
      rename is carried downstream. <a href="richer-graph-review.html" target="_blank" rel="noopener"
      style="color:var(--mint)">See preserve and propagate side by side{arrow}</a></p>
  </div>

  <div class="wrap grid">
{cards}
  </div>
</main>

<footer>
  <div class="wrap">
    Generated from each run's own manifest by <code>scripts/build_examples_index.py</code>.
    <a href="../index.html">Back to the tool</a> &middot;
    <a href="https://github.com/itxcrusher/ripple-proof" target="_blank" rel="noopener">Source</a>
  </div>
</footer>
</body>
</html>
"""


def main() -> None:
    OUTPUT.write_text(render(), encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(PROJECT_ROOT)} from {len(ENTRIES)} captured runs")


if __name__ == "__main__":
    main()
