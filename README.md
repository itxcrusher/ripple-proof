# RippleProof

**Impact analysis is where RippleProof starts, not what it outputs.**

RippleProof is a bounded PostgreSQL column-rename agent. It turns the evidence DataHub can prove into dbt repairs across repositories, executes those repairs in isolation, refuses where the evidence is ambiguous or stale, and stops at human-reviewed pull requests.

## Judge RippleProof in 60 seconds

One campaign renames `analytics.orders.user_id` to `customer_id`. Everything below is bound to manifest SHA-256 `7b907e81...135134a31`.

1. **It generates code that builds, across repositories.** Eight files, four SQL model repairs and four dbt contract updates, across four repositories. All four dbt projects build in isolated DuckDB environments, and each run proves the exact expected models executed.
2. **It refuses rather than guesses.** One DataHub asset matched two candidate dbt implementations, so the campaign failed closed. A reviewer may select only a candidate RippleProof already discovered, and that choice is hash-bound with the rejected candidate kept in the record. A Looker dashboard that is reachable but has no proven column usage gets no invented patch; it stays in manual review.
3. **It detects stale metadata before it acts.** Before any external change, RippleProof re-reads DataHub and independently bypasses the lineage cache. In a controlled test, MCP returned a lineage edge that had been removed while the uncached read did not, and the campaign refused.

Verify the captured chain yourself with Python 3.11 and nothing else. No install, account, credential, Docker service, or network call:

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = 'src'
python -S -m lineage_agent.cli demo
```

Expect `CAMPAIGN AUDIT: PASSED`, 11 of 11 checks. That verifies captured, manifest-bound evidence; it deliberately does not establish that DataHub or GitHub are reachable right now.

Real delivery evidence, open and check-passing: [analytics #6](https://github.com/itxcrusher/ripple-proof-dbt-analytics/pull/6), [finance #6](https://github.com/itxcrusher/ripple-proof-dbt-finance/pull/6), [growth #5](https://github.com/itxcrusher/ripple-proof-dbt-growth/pull/5), [operations #5](https://github.com/itxcrusher/ripple-proof-dbt-operations/pull/5).

## What it does and does not do

Give it a source dataset and a rename; it finds downstream assets and owners, maps dbt models, generates contract-aware repairs, validates the proposed source and downstream changes in isolation, and produces one inspectable review page.

Nothing is applied or merged automatically. RippleProof can open review-only pull requests and record a manifest-bound `approved-for-review` decision in DataHub, but a human still owns the merge and migration. Optional PostgreSQL validation runs only in a disposable container and rolls back; missing lineage remains unknown, never evidence that a change is safe.

Scope is deliberately narrow: PostgreSQL column renames with confirmed dbt consumers. SQL shapes it cannot prove ownership for, including joins, CTEs, set operations, and non-SELECT statements, are routed to manual review rather than repaired speculatively.

## Where automation stops

RippleProof's automated surface is bounded on purpose. The boundary is not a comment in the code; it is a checked corpus of 14 cases that the test suite reruns, covering six shapes it repairs and eight it refuses. Run it with `ripple-proof evaluate --corpus evaluation/corpus.json`.

| Scenario | Decision |
| --- | --- |
| Direct downstream contract, confirmed column evidence | repair |
| Quoted identifiers, with inert text left untouched | repair |
| Declared dbt contract propagation | repair |
| DataHub asset resolved through a dbt manifest alias | repair |
| Code and contracts across two repositories | repair |
| Transitive consumer behind a stable contract boundary | protected, no change needed |
| One asset mapped to multiple dbt models | refuse |
| Complex projection where the output alias cannot be proven | refuse |
| Duplicate destination column in a dbt contract | refuse |
| Model containing only inert references to the column | refuse |
| Dashboard reached without column evidence | refuse |
| Unsupported consumer asset type | refuse |
| Preserve boundary without hop distance | refuse |
| No downstream model found for a confirmed asset | refuse |

A refusal is a first-class outcome recorded in the manifest with its reason, not a silent skip. The point of the corpus is that "it fails closed" is a claim you can rerun, not a promise.

## Inspect a checked campaign

Open [`examples/index.html`](examples/index.html) for the no-login product overview and embedded checked-campaign replay. The checked campaign can also be audited without Docker, DataHub, credentials, network access, or third-party Python packages:

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

The audit returns one verdict over 11 checks covering artifact hashes, captured DataHub MCP reads, complete lineage accounting, owner routing, deterministic review gates, executable dbt validation, disposable PostgreSQL rollback, cache-bypassed context verification with owner and lineage drift refusal, captured GitHub delivery, structured DataHub decision readback, and manual-only safety controls. The HTML page exposes the same captured evidence, lineage graph, generated diffs, validation receipts, and pull requests in one self-contained review.

This offline audit checks the integrity and internal consistency of captured, manifest-bound evidence. It does not establish that the evidence came from an authentic external service, and it does not establish current DataHub or GitHub state. Fresh verification is a separate live step before any external mutation.

## Audit a campaign in pull-request CI

Repositories that check in a RippleProof campaign can verify the integrity and internal consistency of its captured evidence without credentials, Docker, DataHub, network calls, or dependency installation:

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
- A Docker engine with at least 8 GB available (Docker Desktop on Windows or macOS) and a local DataHub quickstart are required for live campaign planning
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

To update an installed checkout, run `git pull --ff-only` and repeat `python -m pip install --upgrade .`. To remove the command, run `python -m pip uninstall ripple-proof`; this leaves the cloned repository, `.env`, and any generated campaign directories untouched so they can be reviewed or removed separately.

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
# Copy DATAHUB_GMS_TOKEN from ~/.datahubenv into .env, then run:
ripple-proof doctor
python -m lineage_agent.demo_setup
ripple-proof plan
```

`ripple-proof doctor` is read-only. It checks the configured endpoint and token without printing secrets, confirms the Docker engine and local DataHub quickstart, starts the DataHub MCP server, and performs one live lineage read. It exits nonzero when any required readiness check fails.

If the readiness check fails:

- **Docker engine:** open Docker Desktop, wait until it reports that the engine is running, run `docker version`, then retry `ripple-proof doctor`.
- **Local DataHub:** run `datahub docker check`. If the stack is missing, run `datahub docker quickstart`; wait for it to become healthy, then retry the doctor.
- **Token or MCP read:** run `datahub init --username datahub --password datahub --token-duration no_expiry --force`, copy the new token from `~/.datahubenv` into `.env`, and retry. The doctor prints the failed boundary and a recovery action without printing the token.

Install the isolated validation dependencies when you want RippleProof to run the proposed repositories against DuckDB:

```powershell
python -m venv .dbt-venv
.\.dbt-venv\Scripts\python.exe -m pip install -r requirements-validation.txt
```

Keeping dbt in its own environment avoids changing the DataHub MCP dependency set.

Set `DATAHUB_GMS_TOKEN` in `.env` for live reads and manifest-bound DataHub decisions. Planning uses the strict deterministic parser by default even when `OPENAI_API_KEY` exists. Model-backed parsing runs only when the operator explicitly supplies `--generator llm`.

### Full operator setup on macOS or Linux

Install Python 3.11 with virtual-environment support and a running Docker engine, then run:

```bash
git clone https://github.com/itxcrusher/ripple-proof.git
cd ripple-proof

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[live]"

export PYTHONUTF8=1
datahub docker quickstart
datahub docker check
datahub init --username datahub --password datahub --token-duration no_expiry --force

cp .env.example .env
# Copy DATAHUB_GMS_TOKEN from ~/.datahubenv into .env, then run:
ripple-proof doctor
python -m lineage_agent.demo_setup
ripple-proof plan
```

For isolated dbt validation on macOS or Linux:

```bash
python3 -m venv .dbt-venv
.dbt-venv/bin/python -m pip install -r requirements-validation.txt
```

## Usage

`ripple-proof --help` exposes the complete operator path through planning, offline audit, isolated validation, review rendering, pull-request publication, context verification, and DataHub decision recording.

```powershell
ripple-proof plan `
  --asset-urn 'urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)' `
  --instruction 'rename user_id to customer_id' `
  --max-hops 2
```

To use model-backed intent parsing explicitly and export the report:

```powershell
ripple-proof plan `
  --generator llm `
  --instruction 'Please change the user identifier from user_id to customer_id' `
  --report-file impact-report.md
```

An API key never changes the default path. DataHub decision recording is a later, manifest-bound step performed with `ripple-proof record-decision` only after the campaign has passed validation and its review pull requests have been freshly verified.

To generate a complete review bundle against a dbt project:

```powershell
$bundlePath = Join-Path $env:TEMP ("ripple-proof-bundle-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
ripple-proof plan `
  --instruction 'rename user_id to customer_id' `
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
- `context-verification.json`: optional manifest-bound cache-bypassed match, owner-drift refusal, and lineage-drift refusal observations
- `postgres-validation.json`: optional manifest-bound disposable PostgreSQL rename and rollback evidence
- `publication.json` and `decision.json`: optional manifest-bound GitHub delivery and fresh-client DataHub decision evidence
- `verification.json`: optional offline integrity and internal-consistency audit bound to the exact campaign manifest

The model returns only a constrained rename intent. Identifier validation, schema validation, SQL rendering, lineage analysis, downstream patching, and remediation rendering remain deterministic. The planning command cannot write to DataHub; structured review decisions use the exact campaign manifest and the dedicated `record-decision` command.

The current implementation supports PostgreSQL column renames and confirmed dbt dataset dependencies. Repeat `--target-repo` to plan a campaign across multiple dbt projects. RippleProof prefers dbt `target/manifest.json` mappings, including model aliases, and falls back to unique model filenames when a manifest is unavailable. It changes executable SQL identifiers while preserving comments, strings, dollar-quoted bodies, and Jinja blocks. Ambiguous model paths, complex output projections, unsupported platforms, and asset-only lineage stay in the manual-review list instead of being guessed. A batched DataHub `get_entities` read resolves the source and downstream owners; the campaign records an assigned or unassigned review route for every disposition.

Generated dbt patches preserve a confirmed one-hop downstream output name by default. For example, a source rename from `user_id` to `customer_id` becomes `customer_id as user_id` in an unambiguous projection while filters and grouping use `customer_id`. Transitive models remain unchanged and are recorded as protected by that compatibility boundary. Use `--downstream-mode propagate` only when the output rename is intentional; this patches every confirmed dbt consumer and updates matching column declarations in dbt schema YAML while preserving their descriptions and tests.

Every campaign runs deterministic validation gates for patch integrity, output-contract invariants, complete lineage accounting, unambiguous DataHub-to-code mapping, and captured DataHub context. A live campaign stores a canonical SHA-256 fingerprint over the source schema, source and downstream ownership, lineage depth, and downstream evidence. Before any operation can push to GitHub or write a DataHub decision, RippleProof repeats the MCP reads and independently queries DataHub lineage with `searchAcrossLineage` and `skipCache: true`. It refuses the mutation when the fingerprint changed or the repeated MCP result differs from the cache-bypassed lineage result. An asset must be classified as patched, protected by an unchanged contract, or assigned to manual review. A DataHub asset that maps to multiple dbt models is a blocking failure, even when other patches are valid. The manifest records whether the campaign is ready for review. RippleProof never edits a target repository or applies the migration to the source database.

`--validate-dbt` is an explicit execution boundary. It copies the target projects into temporary directories, overlays the proposed files, loads a post-rename fixture into a disposable DuckDB database, and runs `dbt seed` plus `dbt build`. The source repositories remain untouched, and the bounded command evidence is recorded in the manifest and campaign page. Because dbt projects may contain executable macros, enable this only for repositories you trust.

`--validate-postgres` is a separate explicit boundary for the generated source DDL. It accepts only the exact deterministic rename for the campaign, starts a locally available PostgreSQL image with no network and tmpfs-backed data, creates a minimal source fixture, observes the old column, applies and observes the rename inside a transaction, rolls it back, observes the restored column, and removes the container. It uses `--pull never`, so the requested image must already exist. A failure blocks review readiness and records bounded command evidence; source and production databases remain unreachable.

`examples/dbt_project/` and `examples/dbt_finance_project/` are the original two repositories represented in the local DataHub graph. `examples/generated/` and `examples/generated-propagate/` preserve that published campaign and its manifest-bound delivery evidence. Open `examples/campaign-review.html` to compare its preserve and propagate outputs without installing dependencies or starting DataHub.

The richer controlled graph adds growth and two operations repository candidates, five downstream assets, and six DataHub ownership routes including the source. Open `examples/richer-graph-review.html` to compare its two policies. Preserve mode keeps the one-hop contract stable and all four selected dbt validation roots pass. Propagate mode generates supported analytics, finance, and growth repairs but refuses to choose between two repository candidates that both map to `customer_identity_export`, so the campaign is marked blocked.

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

Open `examples/ambiguity-resolution-review.html` to inspect the captured transition from blocked ambiguity to an explicitly resolved campaign. The resolved campaign accounts for five repository candidates: four are selected for four SQL and four contract patches, while the shadow operations candidate remains explicitly unselected in the decision record. All four selected dbt validation roots pass and the campaign becomes review-ready.

To compare a captured campaign with the current DataHub graph without publishing or writing metadata:

```powershell
ripple-proof verify-context --bundle resolved-campaign
```

The command repeats the MCP reads, independently bypasses DataHub's lineage search cache, prints the matching SHA-256, and exits nonzero with the schema, ownership, lineage, or MCP-versus-uncached difference when the graph has drifted.

To persist an inspectable observation without changing DataHub, identify the expected result explicitly:

```powershell
ripple-proof verify-context `
  --bundle resolved-campaign `
  --record-evidence `
  --scenario matched
```

Use `--scenario refused-drift` or `--scenario refused-lineage-drift` only while verifying an intentionally changed test graph. The command records the repeated MCP snapshot, the cache-bypassed lineage signatures, and the concrete differences, then still exits with code 2 because the campaign is stale. A scenario is written only when its label agrees with the live result; `--force` is required to replace an existing observation. The sidecar is bound to the exact campaign manifest and captured context fingerprint, and the replay rejects mismatched or internally inconsistent evidence.

The controlled local demo can exercise the complete guard, including the stale-cache case, in one command. This temporarily changes and restores the exact `analytics.orders -> orders_daily` demo ownership and lineage aspects; it refuses any other graph and requires an explicit confirmation flag:

```powershell
ripple-proof prove-context-guard `
  --bundle examples\generated-ripple-proof-resolved `
  --record-evidence `
  --confirm-demo-mutation
```

The command first verifies the unchanged graph, then proves refusal after a temporary owner change and after a temporary lineage removal. It confirms the restored cache-bypassed graph before reporting success and atomically replaces the three-scenario evidence sidecar only after the full regression passes.

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

Before cloning or pushing, RippleProof repeats the source schema, lineage, and ownership reads through DataHub MCP and requires an exact match with the approved context fingerprint. It then clones every mapped repository and verifies every approved source hash before the first push. Only after every preflight succeeds does it create deterministic campaign branches, open review-only pull requests, wait for every explicitly required check, and write manifest-bound evidence. It never enables auto-merge, merges a pull request, or executes the migration.

An external service can fail after one repository succeeds. RippleProof records each completed repository operation against the approved manifest. Retry the exact same approved publish command: it re-clones and verifies every recorded branch, pull request, complete diff, and file hash, then continues only the remaining repositories. Conflicting existing state is refused instead of overwritten.

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

Only that exact campaign can be recorded as approved for human review. Write mode also requires the same repository mappings and required checks used for publication so RippleProof can refresh the complete GitHub state first:

```powershell
ripple-proof record-decision `
  --bundle-dir resolved-campaign `
  --repo-map dbt_project=OWNER/ANALYTICS_REPOSITORY `
  --repo-map dbt_finance_project=OWNER/FINANCE_REPOSITORY `
  --repo-map dbt_growth_project=OWNER/GROWTH_REPOSITORY `
  --repo-map dbt_operations_primary=OWNER/OPERATIONS_REPOSITORY `
  --required-check build `
  --write `
  --approve-manifest-sha256 SHA256_FROM_DRY_RUN
```

Immediately before opening a DataHub writer, the command freshly re-verifies every recorded GitHub branch, complete diff, proposed-file hash, open pull request, and required check, then repeats the MCP context reads. Stale or mismatched GitHub proof and DataHub drift are refused before the first metadata write. It preserves unrelated structured metadata and attaches the same manifest-bound decision to the source and every lineage asset.

A decision write can also stop between assets. Retry the exact same approved command: RippleProof verifies the assets already recorded, preserves the timestamp of the first successful write, and continues only the remaining assets. It then closes the write client, creates a fresh DataHub client, and reads every assignment back before final evidence is accepted. The status is `approved-for-review`; merge remains manual-only and the migration remains unexecuted.

## Tests

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

The unit suite runs without DataHub, network access, or API credentials. Use `ripple-proof doctor` for the complete read-only live readiness check; use `python -m lineage_agent.mcp_test` when you need only the MCP tool and lineage-read probe.

The canonical CI release gates cover:

- the full unit suite plus explicit captured-evidence tampering regressions;
- exact live dependency resolution, `pip check`, and vulnerability auditing for both runtime and validation dependency sets;
- all four selected dbt validation roots in isolated DuckDB workspaces, while accounting for the fifth unselected repository candidate;
- the constrained PostgreSQL rename and rollback in a disposable container; and
- refusal when the destination column already exists.

A green unit suite alone is not a release verdict. The dbt, PostgreSQL, dependency, and tamper jobs must also pass.

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
- `src/lineage_agent/audit.py`: dependency-free integrity and internal-consistency checks for captured evidence
- `src/lineage_agent/replay.py`: dependency-free visual campaign review and mode comparison
- `src/lineage_agent/demo_setup.py`: local sample graph setup
- `src/lineage_agent/doctor.py`: read-only Docker, DataHub quickstart, configuration, and live MCP readiness checks
- `src/lineage_agent/mcp_test.py`: focused live MCP connectivity check
- `tests/`: offline test suite
- `evaluation/`: adversarial safety corpus
- `examples/`: sample dbt input and generated review bundle
