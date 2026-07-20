# BlastRadius

BlastRadius proposes PostgreSQL column renames after checking DataHub for downstream dependencies. It validates the source schema, combines column-level and asset-level lineage evidence, generates deterministic SQL, produces an impact report, and can write the proposed-change context back to affected DataHub assets.

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

The model returns a constrained rename intent. Identifier validation, schema validation, SQL rendering, lineage analysis, and remediation rendering remain deterministic. `--write-back` is explicit and idempotent; it updates BlastRadius's own DataHub entry while preserving unrelated links.

The current implementation supports PostgreSQL column renames. It distinguishes confirmed column dependencies from broader downstream reachability so it can recommend a concrete reference update only when the lineage evidence supports one.

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
- `src/lineage_agent/writeback.py`: opt-in, idempotent DataHub write-back
- `src/lineage_agent/demo_setup.py`: local sample graph setup
- `src/lineage_agent/mcp_test.py`: live MCP connectivity check
- `tests/`: offline test suite
