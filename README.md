# BlastRadius

BlastRadius proposes PostgreSQL column renames after checking DataHub for downstream dependencies. It validates the source schema, combines column-level and asset-level lineage evidence, generates deterministic migration SQL, plans contract-aware repairs across dbt repositories, validates the campaign, and can write the proposed-change context back to affected DataHub assets.

BlastRadius does not execute migrations. Missing lineage is reported as unknown, not as proof that a change is safe.

## Requirements

- Python 3.11
- Docker Desktop with at least 8 GB available
- A local DataHub quickstart

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
  --bundle-dir $bundlePath
```

The bundle directory must not already exist and must be outside the target repository. It contains:

- `migration.sql`: the proposed PostgreSQL migration
- `proposed/`: complete proposed downstream model files
- `patches/`: unified diffs for review or PR use
- `impact-report.md`: DataHub evidence, remediation, generated patches, and unresolved assets
- `manifest.json`: repository mappings, source/proposed hashes, asset dispositions, validation gates, and safety flags
- `campaign.html`: a self-contained visual review of lineage, dispositions, diffs, evidence, and safety gates

The model returns a constrained rename intent. Identifier validation, schema validation, SQL rendering, lineage analysis, downstream patching, and remediation rendering remain deterministic. `--write-back` is explicit and idempotent; it updates BlastRadius's own DataHub entry while preserving unrelated links.

The current implementation supports PostgreSQL column renames and confirmed dbt dataset dependencies. Repeat `--target-repo` to plan a campaign across multiple dbt projects. BlastRadius prefers dbt `target/manifest.json` mappings, including model aliases, and falls back to unique model filenames when a manifest is unavailable. It changes executable SQL identifiers while preserving comments, strings, dollar-quoted bodies, and Jinja blocks. Ambiguous model paths, complex output projections, unsupported platforms, and asset-only lineage stay in the manual-review list instead of being guessed.

Generated dbt patches preserve a confirmed one-hop downstream output name by default. For example, a source rename from `user_id` to `customer_id` becomes `customer_id as user_id` in an unambiguous projection while filters and grouping use `customer_id`. Transitive models remain unchanged and are recorded as protected by that compatibility boundary. Use `--downstream-mode propagate` only when the output rename is intentional; this patches every confirmed dbt consumer and updates matching column declarations in dbt schema YAML while preserving their descriptions and tests.

Every campaign runs deterministic validation gates for patch integrity, output-contract invariants, and complete lineage accounting. An asset must be classified as patched, protected by an unchanged contract, or assigned to manual review. The manifest records whether the campaign is ready for review. BlastRadius never edits a target repository or executes the migration.

`examples/dbt_project/` and `examples/dbt_finance_project/` are two repositories represented in the local DataHub graph. `examples/generated/` and `examples/generated-propagate/` are complete campaigns captured from that graph. Open `examples/campaign-review.html` to compare their real preserve and propagate outputs without installing dependencies or starting DataHub.

To build the same self-contained comparison from any two bundles:

```powershell
python -m lineage_agent.replay `
  --bundle examples\generated `
  --bundle examples\generated-propagate `
  --output campaign-review.html
```

The page embeds the manifests and patch text at export time. It makes no network requests and does not execute generated SQL.

## Tests

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

The unit suite runs without DataHub, network access, or API credentials. Use `python -m lineage_agent.mcp_test` for a live MCP connectivity check.

## Source layout

- `src/lineage_agent/main.py`: CLI, DataHub MCP client, lineage analysis, and deterministic SQL generation
- `src/lineage_agent/llm.py`: constrained OpenAI structured-output intent parsing
- `src/lineage_agent/reporting.py`: Markdown impact-report rendering and export
- `src/lineage_agent/campaign.py`: multi-repository discovery, topology-aware planning, validation, and bundle manifests
- `src/lineage_agent/bundling.py`: SQL-aware dbt model patching
- `src/lineage_agent/contracts.py`: round-trip-safe dbt schema contract patching
- `src/lineage_agent/replay.py`: dependency-free visual campaign review and mode comparison
- `src/lineage_agent/writeback.py`: opt-in, idempotent DataHub write-back
- `src/lineage_agent/demo_setup.py`: local sample graph setup
- `src/lineage_agent/mcp_test.py`: live MCP connectivity check
- `tests/`: offline test suite
- `examples/`: sample dbt input and generated review bundle
