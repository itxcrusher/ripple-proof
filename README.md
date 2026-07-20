# BlastRadius

BlastRadius proposes PostgreSQL column renames after checking DataHub for downstream dependencies. It validates the source schema, combines column-level and asset-level lineage evidence, generates deterministic migration SQL, patches confirmed downstream dbt models, produces a review bundle, and can write the proposed-change context back to affected DataHub assets.

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
python -m lineage_agent.main `
  --generator llm `
  --instruction 'Please change the user identifier from user_id to customer_id' `
  --target-repo examples/dbt_project `
  --bundle-dir "$env:TEMP\blastradius-change-bundle"
```

The bundle directory must not already exist and must be outside the target repository. It contains:

- `migration.sql`: the proposed PostgreSQL migration
- `proposed/`: complete proposed downstream model files
- `patches/`: unified diffs for review or PR use
- `impact-report.md`: DataHub evidence, remediation, generated patches, and unresolved assets
- `manifest.json`: paths, source/proposed hashes, replacement counts, validation state, and safety flags

The model returns a constrained rename intent. Identifier validation, schema validation, SQL rendering, lineage analysis, downstream patching, and remediation rendering remain deterministic. `--write-back` is explicit and idempotent; it updates BlastRadius's own DataHub entry while preserving unrelated links.

The current implementation supports PostgreSQL column renames and confirmed dbt dataset dependencies. It changes executable SQL identifiers while preserving comments, strings, dollar-quoted bodies, and Jinja blocks. Ambiguous model paths, unsupported platforms, and asset-only lineage stay in the manual-review list instead of being guessed.

Generated dbt patches propagate the renamed column through executable references. That can change the downstream model's output contract, so every bundle records this explicitly and requires human review. BlastRadius never edits the target repository or executes the migration.

`examples/dbt_project/` is the input used by the local DataHub graph. `examples/generated/` is a complete OpenAI-backed bundle produced by the same CLI against the live graph.

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
- `src/lineage_agent/bundling.py`: dbt model discovery, SQL-aware patching, diffs, and bundle manifests
- `src/lineage_agent/writeback.py`: opt-in, idempotent DataHub write-back
- `src/lineage_agent/demo_setup.py`: local sample graph setup
- `src/lineage_agent/mcp_test.py`: live MCP connectivity check
- `tests/`: offline test suite
- `examples/`: sample dbt input and generated review bundle
