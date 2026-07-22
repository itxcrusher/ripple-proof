# BlastRadius

BlastRadius proposes PostgreSQL column renames after checking DataHub for downstream dependencies. It validates the source schema, combines column-level and asset-level lineage evidence, resolves DataHub owners for review routing, generates deterministic migration SQL, plans contract-aware repairs across dbt repositories, validates the campaign, and can write the proposed-change context back to affected DataHub assets.

BlastRadius does not execute migrations. Missing lineage is reported as unknown, not as proof that a change is safe.

## Requirements

- Python 3.11
- Docker Desktop with at least 8 GB available
- A local DataHub quickstart
- dbt Core and dbt-duckdb are optional and only required for executable campaign validation
- GitHub CLI, authenticated with repository access, is optional and only required for pull-request publication

## Setup

### Windows PowerShell

```powershell
git clone https://github.com/itxcrusher/blastradius.git
Set-Location blastradius

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

$env:PYTHONUTF8 = '1'
datahub docker quickstart
datahub docker check
datahub init --username datahub --password datahub --token-duration no_expiry --force

Copy-Item .env.example .env
$env:PYTHONPATH = 'src'
python -m lineage_agent.demo_setup
python -m lineage_agent.main
```

Install the isolated validation dependencies when you want BlastRadius to run the proposed repositories against DuckDB:

```powershell
python -m venv .dbt-venv
.\.dbt-venv\Scripts\python.exe -m pip install -r requirements-validation.txt
```

Keeping dbt in its own environment avoids changing the DataHub MCP dependency set.

For write-back, set `DATAHUB_GMS_TOKEN` in `.env`. `OPENAI_API_KEY` is optional: the default `auto` generator falls back to the strict local parser when no key is configured.

### macOS or Linux

Use the same commands with these environment-specific replacements:

```bash
source .venv/bin/activate
cp .env.example .env
export PYTHONPATH=src
```

## Usage

```powershell
python -m lineage_agent.main `
  --asset-urn 'urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)' `
  --instruction 'rename user_id to customer_id' `
  --max-hops 2
```

To use model-backed intent parsing, export the report, and write the context back to DataHub:

```powershell
python -m lineage_agent.main `
  --generator llm `
  --instruction 'Please change the user identifier from user_id to customer_id' `
  --write-back `
  --report-file impact-report.md
```

To generate a complete review bundle against a dbt project:

```powershell
$bundlePath = Join-Path $env:TEMP ("blastradius-bundle-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
python -m lineage_agent.main `
  --generator llm `
  --instruction 'Please change the user identifier from user_id to customer_id' `
  --target-repo examples/dbt_project `
  --target-repo examples/dbt_finance_project `
  --bundle-dir $bundlePath `
  --validate-dbt `
  --dbt-executable .\.dbt-venv\Scripts\dbt.exe
```

The bundle directory must not already exist and must be outside the target repository. It contains:

- `migration.sql`: the proposed PostgreSQL migration
- `proposed/`: complete proposed downstream model files
- `patches/`: unified diffs for review or PR use
- `impact-report.md`: DataHub evidence, remediation, generated patches, and unresolved assets
- `manifest.json`: repository mappings, source/proposed hashes, asset dispositions, owner-based review assignments, validation gates, and safety flags
- `campaign.html`: a self-contained visual review of lineage, review routing, dispositions, diffs, evidence, and safety gates

The model returns a constrained rename intent. Identifier validation, schema validation, SQL rendering, lineage analysis, downstream patching, and remediation rendering remain deterministic. `--write-back` is explicit and idempotent; it updates BlastRadius's own DataHub entry while preserving unrelated links.

The current implementation supports PostgreSQL column renames and confirmed dbt dataset dependencies. Repeat `--target-repo` to plan a campaign across multiple dbt projects. BlastRadius prefers dbt `target/manifest.json` mappings, including model aliases, and falls back to unique model filenames when a manifest is unavailable. It changes executable SQL identifiers while preserving comments, strings, dollar-quoted bodies, and Jinja blocks. Ambiguous model paths, complex output projections, unsupported platforms, and asset-only lineage stay in the manual-review list instead of being guessed. A batched DataHub `get_entities` read resolves the source and downstream owners; the campaign records an assigned or unassigned review route for every disposition.

Generated dbt patches preserve a confirmed one-hop downstream output name by default. For example, a source rename from `user_id` to `customer_id` becomes `customer_id as user_id` in an unambiguous projection while filters and grouping use `customer_id`. Transitive models remain unchanged and are recorded as protected by that compatibility boundary. Use `--downstream-mode propagate` only when the output rename is intentional; this patches every confirmed dbt consumer and updates matching column declarations in dbt schema YAML while preserving their descriptions and tests.

Every campaign runs deterministic validation gates for patch integrity, output-contract invariants, complete lineage accounting, and unambiguous DataHub-to-code mapping. An asset must be classified as patched, protected by an unchanged contract, or assigned to manual review. A DataHub asset that maps to multiple dbt models is a blocking failure, even when other patches are valid. The manifest records whether the campaign is ready for review. BlastRadius never edits a target repository or executes the migration.

`--validate-dbt` is an explicit execution boundary. It copies the target projects into temporary directories, overlays the proposed files, loads a post-rename fixture into a disposable DuckDB database, and runs `dbt seed` plus `dbt build`. The source repositories remain untouched, and the bounded command evidence is recorded in the manifest and campaign page. Because dbt projects may contain executable macros, enable this only for repositories you trust.

`examples/dbt_project/` and `examples/dbt_finance_project/` are the original two repositories represented in the local DataHub graph. `examples/generated/` and `examples/generated-propagate/` preserve that published campaign and its manifest-bound delivery evidence. Open `examples/campaign-review.html` to compare its preserve and propagate outputs without installing dependencies or starting DataHub.

The richer controlled graph adds growth and two operations repositories, five downstream assets, and six DataHub ownership routes including the source. Open `examples/richer-graph-review.html` to compare its two policies. Preserve mode keeps the one-hop contract stable and all four isolated dbt validation roots pass. Propagate mode generates supported analytics, finance, and growth repairs but refuses to choose between two repositories that both map to `customer_identity_export`, so the campaign is marked blocked.

A reviewer can explicitly resolve that ambiguity by selecting one exact candidate discovered by BlastRadius. `--model-map` rejects unknown assets, unambiguous assets, duplicate choices, and paths outside the discovered candidate set. The resulting manifest records the selected and unselected candidates plus a SHA-256 of the decision; every supplied dbt validation root still runs.

```powershell
$identityUrn = 'urn:li:dataset:(urn:li:dataPlatform:dbt,customer_identity_export,PROD)'

python -m lineage_agent.main `
  --target-repo examples\dbt_project `
  --target-repo examples\dbt_finance_project `
  --target-repo examples\dbt_growth_project `
  --target-repo examples\dbt_operations_primary `
  --target-repo examples\dbt_operations_shadow `
  --bundle-dir resolved-campaign `
  --downstream-mode propagate `
  --model-map "$identityUrn=dbt_operations_primary/models/customer_identity_primary.sql"
```

Open `examples/ambiguity-resolution-review.html` to inspect the captured transition from blocked ambiguity to an explicitly resolved campaign. The resolved campaign contains four SQL and four contract patches, retains the unselected shadow candidate in its decision record, passes all four isolated dbt roots, and becomes review-ready.

To build the same self-contained comparison from any two bundles:

```powershell
python -m lineage_agent.replay `
  --bundle examples\generated `
  --bundle examples\generated-propagate `
  --output campaign-review.html
```

The page embeds the manifests and patch text at export time. It makes no network requests and does not execute generated SQL.

## Adversarial evaluation

BlastRadius includes a deterministic safety corpus covering both supported repairs and cases that must remain manual. It measures repair coverage, manual-review recall, and false repairs while also proving that source repositories were not modified.

```powershell
python -m lineage_agent.evaluation `
  --corpus evaluation\corpus.json
```

To reproduce the checked JSON evidence and self-contained HTML report in another directory:

```powershell
python -m lineage_agent.evaluation `
  --corpus evaluation\corpus.json `
  --output-dir $env:TEMP\blastradius-evaluation
```

The output directory must not already contain `report.json` or `report.html` unless `--force` is explicitly supplied. The checked report is available at `examples/evaluation/report.html`; it makes no network requests and can be opened directly in a browser.

To independently re-run the executable proof stored in a checked-in campaign:

```powershell
python -m lineage_agent.dbt_validation `
  --bundle examples\generated-propagate `
  --target-repo examples\dbt_project `
  --target-repo examples\dbt_finance_project `
  --dbt-executable .\.dbt-venv\Scripts\dbt.exe
```

## Opening review pull requests

GitHub publication is a separate, explicit boundary. A dry-run validates the campaign, groups its files by repository, and prints the exact manifest SHA-256 that a reviewer must approve:

```powershell
python -m lineage_agent.github_publication `
  --bundle examples\generated-propagate `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY
```

Only the same immutable campaign can then be published:

```powershell
python -m lineage_agent.github_publication `
  --bundle examples\generated-propagate `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY `
  --publish `
  --required-check build `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

BlastRadius clones every mapped repository and verifies every approved source hash before the first push. It then creates deterministic campaign branches, opens review-only pull requests, waits for every explicitly required check, and writes manifest-bound evidence to `publication.json`. It never enables auto-merge, merges a pull request, or executes the migration. Existing campaign branches are not overwritten.

If checks finish asynchronously, use `--refresh` with the same repository mappings, manifest approval hash, and required checks. Refresh mode clones the existing campaign branches, proves that their complete base diff and proposed-file hashes still match the approved bundle, then updates `publication.json` and `campaign.html` without pushing code.

```powershell
python -m lineage_agent.github_publication `
  --bundle examples\generated-propagate `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY `
  --refresh `
  --required-check build `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

## Recording the DataHub review decision

After publication evidence exists, BlastRadius can plan a structured DataHub decision without changing metadata. The dry-run prints the exact manifest hash and approval scope:

```powershell
python -m lineage_agent.decision `
  --bundle-dir examples\generated-propagate
```

Only that exact campaign can be recorded as approved for human review:

```powershell
python -m lineage_agent.decision `
  --bundle-dir examples\generated-propagate `
  --write `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

The command creates DataHub structured-property definitions, preserves unrelated structured metadata, and attaches the same manifest-bound decision to the source and every lineage asset. It closes the write client, creates a fresh DataHub client, reads every assignment back, and writes `decision.json` only after all assets match. The status is `approved-for-review`; merge remains manual-only and the migration remains unexecuted.

## Tests

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

The unit suite runs without DataHub, network access, or API credentials. Use `python -m lineage_agent.mcp_test` for a live MCP connectivity check.

The separate dbt integration check requires `requirements-validation.txt` and executes both checked-in campaigns against DuckDB.

## Source layout

- `src/lineage_agent/main.py`: CLI, DataHub MCP client, lineage analysis, and deterministic SQL generation
- `src/lineage_agent/llm.py`: constrained OpenAI structured-output intent parsing
- `src/lineage_agent/reporting.py`: Markdown impact-report rendering and export
- `src/lineage_agent/campaign.py`: multi-repository discovery, topology-aware planning, validation, and bundle manifests
- `src/lineage_agent/bundling.py`: SQL-aware dbt model patching
- `src/lineage_agent/contracts.py`: round-trip-safe dbt schema contract patching
- `src/lineage_agent/dbt_validation.py`: isolated DuckDB-backed execution and reproducible evidence
- `src/lineage_agent/github_publication.py`: approval-bound, review-only GitHub pull request delivery
- `src/lineage_agent/decision.py`: manifest-bound DataHub review decisions and fresh-session verification
- `src/lineage_agent/evaluation.py`: deterministic adversarial corpus runner and evidence renderer
- `src/lineage_agent/replay.py`: dependency-free visual campaign review and mode comparison
- `src/lineage_agent/writeback.py`: opt-in, idempotent DataHub write-back
- `src/lineage_agent/demo_setup.py`: local sample graph setup
- `src/lineage_agent/mcp_test.py`: live MCP connectivity check
- `tests/`: offline test suite
- `evaluation/`: adversarial safety corpus
- `examples/`: sample dbt input and generated review bundle
