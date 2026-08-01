# RippleProof

RippleProof turns a risky PostgreSQL column rename into a reviewable change campaign. It checks DataHub for downstream dependencies, resolves owners, generates contract-aware dbt repairs, validates the source and downstream changes in isolation, opens review-only pull requests, and can record the approved decision back in DataHub.

RippleProof never executes migrations against the source or a production database. Optional validation applies the constrained rename only inside a disposable PostgreSQL container and rolls it back. Missing lineage is reported as unknown, not as proof that a change is safe.

## See the complete proof

Open [`examples/index.html`](examples/index.html) for the no-login product overview and embedded verified campaign. The checked campaign can also be audited without Docker, DataHub, credentials, network access, or third-party Python packages:

```powershell
$env:PYTHONPATH = 'src'
python -S -m lineage_agent.audit `
  --bundle examples\generated-ripple-proof-resolved

Start-Process examples\generated-ripple-proof-resolved\campaign.html
```

After installation, the same path is one command:

```powershell
ripple-proof demo --open
```

The audit returns one verdict over 11 checks covering artifact hashes, DataHub MCP capture, complete lineage accounting, owner routing, deterministic review gates, executable dbt validation, disposable PostgreSQL rollback, fresh-context match and drift refusal, check-passing GitHub delivery, structured DataHub decision readback, and manual-only safety controls. The HTML page exposes the same proof chain, lineage graph, generated diffs, validation evidence, and pull requests in one self-contained review.

This is an offline verification of captured, manifest-bound evidence. It does not claim that DataHub or GitHub was queried again at review time.

## Audit a campaign in pull-request CI

Repositories that check in a RippleProof campaign can verify its complete captured proof chain without credentials, Docker, DataHub, network calls, or dependency installation:

```yaml
name: RippleProof campaign audit

on:
  pull_request:

permissions:
  contents: read

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
      - uses: itxcrusher/ripple-proof/.github/actions/audit-campaign@main
        with:
          bundle: path/to/checked-campaign
```

Pin the action to a release tag or commit for production use. The action fails closed when the manifest, generated files, validation receipts, publication evidence, or DataHub decision evidence is missing or inconsistent.

## Requirements

- Python 3.11 is the only requirement for the checked campaign demo
- Docker Desktop with at least 8 GB available and a local DataHub quickstart are required for live campaign planning
- dbt Core and dbt-duckdb are optional and only required for executable campaign validation
- GitHub CLI, authenticated with repository access, is optional and only required for pull-request publication

## Setup

### Reviewer quick start

Clone and audit the complete checked campaign without creating an environment or installing anything:

```powershell
git clone https://github.com/itxcrusher/ripple-proof.git
Set-Location ripple-proof

$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'src'
python -S -m lineage_agent.cli demo
Start-Process examples\generated-ripple-proof-resolved\campaign.html
```

The macOS or Linux equivalent is `PYTHONPATH=src python -S -m lineage_agent.cli demo`. The live DataHub and OpenAI integrations are not installed or contacted by this path.

For the installed `ripple-proof demo --open` command, create a virtual environment and run `python -m pip install .`. The base package has no runtime dependencies; operator and validation dependencies remain explicit extras.

### Full operator setup on Windows PowerShell

```powershell
git clone https://github.com/itxcrusher/ripple-proof.git
Set-Location ripple-proof

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[live]"

$env:PYTHONUTF8 = '1'
datahub docker quickstart
datahub docker check
datahub init --username datahub --password datahub --token-duration no_expiry --force

Copy-Item .env.example .env
python -m lineage_agent.demo_setup
ripple-proof plan
```

Install the isolated validation dependencies when you want RippleProof to run the proposed repositories against DuckDB:

```powershell
python -m venv .dbt-venv
.\.dbt-venv\Scripts\python.exe -m pip install -r requirements-validation.txt
```

Keeping dbt in its own environment avoids changing the DataHub MCP dependency set.

For write-back, set `DATAHUB_GMS_TOKEN` in `.env`. `OPENAI_API_KEY` is optional: the default `auto` generator falls back to the strict local parser when no key is configured.

### Full operator setup on macOS or Linux

Use the same commands with these environment-specific replacements:

```bash
source .venv/bin/activate
cp .env.example .env
export PYTHONPATH=src
```

## Usage

`ripple-proof --help` exposes the complete operator path through planning, offline audit, isolated validation, review rendering, pull-request publication, context verification, and DataHub decision recording.

```powershell
ripple-proof plan `
  --asset-urn 'urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)' `
  --instruction 'rename user_id to customer_id' `
  --max-hops 2
```

To use model-backed intent parsing, export the report, and write the context back to DataHub:

```powershell
ripple-proof plan `
  --generator llm `
  --instruction 'Please change the user identifier from user_id to customer_id' `
  --write-back `
  --report-file impact-report.md
```

To generate a complete review bundle against a dbt project:

```powershell
$bundlePath = Join-Path $env:TEMP ("ripple-proof-bundle-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
ripple-proof plan `
  --generator llm `
  --instruction 'Please change the user identifier from user_id to customer_id' `
  --target-repo examples/dbt_project `
  --target-repo examples/dbt_finance_project `
  --bundle-dir $bundlePath `
  --validate-postgres `
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
- `context-verification.json`: optional manifest-bound fresh-context match and drift-refusal observations
- `postgres-validation.json`: optional manifest-bound disposable PostgreSQL rename and rollback evidence
- `publication.json` and `decision.json`: optional manifest-bound GitHub delivery and fresh-client DataHub decision evidence
- `verification.json`: optional offline proof-chain audit bound to the exact campaign manifest

The model returns a constrained rename intent. Identifier validation, schema validation, SQL rendering, lineage analysis, downstream patching, and remediation rendering remain deterministic. `--write-back` is explicit and idempotent; it updates RippleProof's own DataHub entry while preserving unrelated links.

The current implementation supports PostgreSQL column renames and confirmed dbt dataset dependencies. Repeat `--target-repo` to plan a campaign across multiple dbt projects. RippleProof prefers dbt `target/manifest.json` mappings, including model aliases, and falls back to unique model filenames when a manifest is unavailable. It changes executable SQL identifiers while preserving comments, strings, dollar-quoted bodies, and Jinja blocks. Ambiguous model paths, complex output projections, unsupported platforms, and asset-only lineage stay in the manual-review list instead of being guessed. A batched DataHub `get_entities` read resolves the source and downstream owners; the campaign records an assigned or unassigned review route for every disposition.

Generated dbt patches preserve a confirmed one-hop downstream output name by default. For example, a source rename from `user_id` to `customer_id` becomes `customer_id as user_id` in an unambiguous projection while filters and grouping use `customer_id`. Transitive models remain unchanged and are recorded as protected by that compatibility boundary. Use `--downstream-mode propagate` only when the output rename is intentional; this patches every confirmed dbt consumer and updates matching column declarations in dbt schema YAML while preserving their descriptions and tests.

Every campaign runs deterministic validation gates for patch integrity, output-contract invariants, complete lineage accounting, unambiguous DataHub-to-code mapping, and captured DataHub context. A live campaign stores a canonical SHA-256 fingerprint over the source schema, source and downstream ownership, lineage depth, and downstream evidence. Immediately before GitHub publication or a structured DataHub decision write, RippleProof repeats the MCP reads and refuses the mutation if that context changed. An asset must be classified as patched, protected by an unchanged contract, or assigned to manual review. A DataHub asset that maps to multiple dbt models is a blocking failure, even when other patches are valid. The manifest records whether the campaign is ready for review. RippleProof never edits a target repository or applies the migration to the source database.

`--validate-dbt` is an explicit execution boundary. It copies the target projects into temporary directories, overlays the proposed files, loads a post-rename fixture into a disposable DuckDB database, and runs `dbt seed` plus `dbt build`. The source repositories remain untouched, and the bounded command evidence is recorded in the manifest and campaign page. Because dbt projects may contain executable macros, enable this only for repositories you trust.

`--validate-postgres` is a separate explicit boundary for the generated source DDL. It accepts only the exact deterministic rename for the campaign, starts a locally available PostgreSQL image with no network and tmpfs-backed data, creates a minimal source fixture, observes the old column, applies and observes the rename inside a transaction, rolls it back, observes the restored column, and removes the container. It uses `--pull never`, so the requested image must already exist. A failure blocks review readiness and records bounded command evidence; source and production databases remain unreachable.

`examples/dbt_project/` and `examples/dbt_finance_project/` are the original two repositories represented in the local DataHub graph. `examples/generated/` and `examples/generated-propagate/` preserve that published campaign and its manifest-bound delivery evidence. Open `examples/campaign-review.html` to compare its preserve and propagate outputs without installing dependencies or starting DataHub.

The richer controlled graph adds growth and two operations repositories, five downstream assets, and six DataHub ownership routes including the source. Open `examples/richer-graph-review.html` to compare its two policies. Preserve mode keeps the one-hop contract stable and all four isolated dbt validation roots pass. Propagate mode generates supported analytics, finance, and growth repairs but refuses to choose between two repositories that both map to `customer_identity_export`, so the campaign is marked blocked.

A reviewer can explicitly resolve that ambiguity by selecting one exact candidate discovered by RippleProof. `--model-map` rejects unknown assets, unambiguous assets, duplicate choices, and paths outside the discovered candidate set. The resulting manifest records the selected and unselected candidates plus a SHA-256 of the decision; every supplied dbt validation root still runs.

```powershell
$identityUrn = 'urn:li:dataset:(urn:li:dataPlatform:dbt,customer_identity_export,PROD)'

ripple-proof plan `
  --target-repo examples\dbt_project `
  --target-repo examples\dbt_finance_project `
  --target-repo examples\dbt_growth_project `
  --target-repo examples\dbt_operations_primary `
  --target-repo examples\dbt_operations_shadow `
  --bundle-dir resolved-campaign `
  --downstream-mode propagate `
  --model-map "$identityUrn=dbt_operations_primary/models/customer_identity_primary.sql" `
  --validate-dbt `
  --dbt-executable .\.dbt-venv\Scripts\dbt.exe
```

Open `examples/ambiguity-resolution-review.html` to inspect the captured transition from blocked ambiguity to an explicitly resolved campaign. The resolved campaign contains four SQL and four contract patches, retains the unselected shadow candidate in its decision record, passes all four isolated dbt roots, and becomes review-ready.

To compare a captured campaign with the current DataHub graph without publishing or writing metadata:

```powershell
ripple-proof verify-context --bundle resolved-campaign
```

The command repeats the MCP reads, prints the matching SHA-256, and exits nonzero with the schema, ownership, or lineage difference when the graph has drifted.

To persist an inspectable observation without changing DataHub, identify the expected result explicitly:

```powershell
ripple-proof verify-context `
  --bundle resolved-campaign `
  --record-evidence `
  --scenario matched
```

Use `--scenario refused-drift` only while verifying an intentionally changed test graph. The command records the fresh snapshot and concrete differences, then still exits with code 2 because the campaign is stale. A scenario is written only when its label agrees with the live result; `--force` is required to replace an existing observation. The sidecar is bound to the exact campaign manifest and captured context fingerprint, and the replay rejects mismatched or internally inconsistent evidence.

To build the same self-contained comparison from any two bundles:

```powershell
ripple-proof review `
  --bundle examples\generated `
  --bundle examples\generated-propagate `
  --output campaign-review.html
```

The page embeds the manifests and patch text at export time. It makes no network requests and does not execute generated SQL.

## Adversarial evaluation

RippleProof includes a deterministic safety corpus covering both supported repairs and cases that must remain manual. It measures repair coverage, manual-review recall, and false repairs while also proving that source repositories were not modified.

```powershell
ripple-proof evaluate `
  --corpus evaluation\corpus.json
```

To reproduce the checked JSON evidence and self-contained HTML report in another directory:

```powershell
ripple-proof evaluate `
  --corpus evaluation\corpus.json `
  --output-dir $env:TEMP\ripple-proof-evaluation
```

The output directory must not already contain `report.json` or `report.html` unless `--force` is explicitly supplied. The checked report is available at `examples/evaluation/report.html`; it makes no network requests and can be opened directly in a browser.

To independently re-run the executable proof stored in a checked-in campaign:

```powershell
ripple-proof validate-dbt `
  --bundle examples\generated-propagate `
  --target-repo examples\dbt_project `
  --target-repo examples\dbt_finance_project `
  --dbt-executable .\.dbt-venv\Scripts\dbt.exe
```

To independently validate and record the checked campaign's source migration without changing its manifest:

```powershell
ripple-proof validate-postgres `
  --bundle examples\generated-ripple-proof-resolved `
  --record-evidence
```

The sidecar is bound to the exact manifest and migration hashes. Use `--force` only when deliberately replacing an earlier observation.

## Opening review pull requests

GitHub publication is a separate, explicit boundary. A dry-run validates the campaign, groups its files by repository, and prints the exact manifest SHA-256 that a reviewer must approve:

```powershell
ripple-proof publish `
  --bundle resolved-campaign `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY `
  --repo-map dbt_growth_project=OWNER/GROWTH_REPOSITORY `
  --repo-map dbt_operations_primary=OWNER/OPERATIONS_REPOSITORY
```

Only the same immutable campaign can then be published:

```powershell
ripple-proof publish `
  --bundle resolved-campaign `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY `
  --repo-map dbt_growth_project=OWNER/GROWTH_REPOSITORY `
  --repo-map dbt_operations_primary=OWNER/OPERATIONS_REPOSITORY `
  --publish `
  --required-check build `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

Before cloning or pushing, RippleProof repeats the source schema, lineage, and ownership reads through DataHub MCP and requires an exact match with the approved context fingerprint. It then clones every mapped repository and verifies every approved source hash before the first push. Only after every preflight succeeds does it create deterministic campaign branches, open review-only pull requests, wait for every explicitly required check, and write manifest-bound evidence to `publication.json`. It never enables auto-merge, merges a pull request, or executes the migration. Existing campaign branches are not overwritten.

If checks finish asynchronously, use `--refresh` with the same repository mappings, manifest approval hash, and required checks. Refresh mode clones the existing campaign branches, proves that their complete base diff and proposed-file hashes still match the approved bundle, then updates `publication.json` and `campaign.html` without pushing code.

```powershell
ripple-proof publish `
  --bundle resolved-campaign `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY `
  --repo-map dbt_growth_project=OWNER/GROWTH_REPOSITORY `
  --repo-map dbt_operations_primary=OWNER/OPERATIONS_REPOSITORY `
  --refresh `
  --required-check build `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

## Recording the DataHub review decision

After publication evidence exists, RippleProof can plan a structured DataHub decision without changing metadata. The dry-run prints the exact manifest hash and approval scope:

```powershell
ripple-proof record-decision `
  --bundle-dir resolved-campaign
```

Only that exact campaign can be recorded as approved for human review:

```powershell
ripple-proof record-decision `
  --bundle-dir resolved-campaign `
  --write `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

The command first repeats the MCP context reads and stops before opening a DataHub writer if the fingerprint changed. It then creates DataHub structured-property definitions, preserves unrelated structured metadata, and attaches the same manifest-bound decision to the source and every lineage asset. It closes the write client, creates a fresh DataHub client, reads every assignment back, and writes `decision.json` only after all assets match. The status is `approved-for-review`; merge remains manual-only and the migration remains unexecuted.

## Tests

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

The unit suite runs without DataHub, network access, or API credentials. Use `python -m lineage_agent.mcp_test` for a live MCP connectivity check.

The separate dbt integration check requires `requirements-validation.txt` and executes both checked-in campaigns against DuckDB.

## Source layout

- `src/lineage_agent/cli.py`: installable `ripple-proof` command and checked-campaign demo path
- `src/lineage_agent/main.py`: CLI, DataHub MCP client, lineage analysis, and deterministic SQL generation
- `src/lineage_agent/llm.py`: constrained OpenAI structured-output intent parsing
- `src/lineage_agent/reporting.py`: Markdown impact-report rendering and export
- `src/lineage_agent/campaign.py`: multi-repository discovery, topology-aware planning, validation, and bundle manifests
- `src/lineage_agent/bundling.py`: SQL-aware dbt model patching
- `src/lineage_agent/contracts.py`: round-trip-safe dbt schema contract patching
- `src/lineage_agent/context_guard.py`: canonical DataHub context capture and pre-mutation drift refusal
- `src/lineage_agent/dbt_validation.py`: isolated DuckDB-backed execution and reproducible evidence
- `src/lineage_agent/postgres_validation.py`: constrained, networkless PostgreSQL rename and rollback validation
- `src/lineage_agent/github_publication.py`: approval-bound, review-only GitHub pull request delivery
- `src/lineage_agent/decision.py`: manifest-bound DataHub review decisions and fresh-session verification
- `src/lineage_agent/evaluation.py`: deterministic adversarial corpus runner and evidence renderer
- `src/lineage_agent/audit.py`: dependency-free offline verification of the complete captured proof chain
- `src/lineage_agent/replay.py`: dependency-free visual campaign review and mode comparison
- `src/lineage_agent/writeback.py`: opt-in, idempotent DataHub write-back
- `src/lineage_agent/demo_setup.py`: local sample graph setup
- `src/lineage_agent/mcp_test.py`: live MCP connectivity check
- `tests/`: offline test suite
- `evaluation/`: adversarial safety corpus
- `examples/`: sample dbt input and generated review bundle
