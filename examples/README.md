# Examples

Start here: **[`index.html`](index.html)** is the product front door, and **[`generated-ripple-proof-resolved/`](generated-ripple-proof-resolved/)** is the campaign this project submits.

Hosted, with nothing to install: <https://itxcrusher.github.io/ripple-proof/>

## The submitted campaign

[`generated-ripple-proof-resolved/`](generated-ripple-proof-resolved/) renames `analytics.orders.user_id` to `customer_id`, bound to manifest SHA-256 `7b907e81...135134a31`.

| Path | What it holds |
| --- | --- |
| [`campaign.html`](generated-ripple-proof-resolved/campaign.html) | The whole campaign as one self-contained page: lineage, dispositions, diffs, validation, delivery, decision |
| [`proposed/`](generated-ripple-proof-resolved/proposed/) | The 8 generated files, complete, across 4 repositories |
| [`patches/`](generated-ripple-proof-resolved/patches/) | The same changes as unified diffs |
| [`impact-report.md`](generated-ripple-proof-resolved/impact-report.md) | DataHub evidence, per-asset remediation, unresolved assets |
| [`manifest.json`](generated-ripple-proof-resolved/manifest.json) | Repository mappings, hashes, dispositions, review routes, validation gates, safety flags |
| [`postgres-validation.json`](generated-ripple-proof-resolved/postgres-validation.json) | Disposable PostgreSQL rename, rollback, and restore observations |
| [`context-verification.json`](generated-ripple-proof-resolved/context-verification.json) | Cache-bypassed match, refused owner drift, refused removed lineage |
| [`publication.json`](generated-ripple-proof-resolved/publication.json) | The four review-only pull requests and their required checks |
| [`decision.json`](generated-ripple-proof-resolved/decision.json) | The `approved-for-review` decision, read back across six DataHub assets |
| [`verification.json`](generated-ripple-proof-resolved/verification.json) | The 11-check offline audit receipt |

Re-verify it yourself with Python 3.11 and nothing else, from the repository root:

```bash
PYTHONPATH=src python -S -m lineage_agent.cli demo
```

## The input projects

[`dbt_project/`](dbt_project/), [`dbt_finance_project/`](dbt_finance_project/), [`dbt_growth_project/`](dbt_growth_project/), [`dbt_operations_primary/`](dbt_operations_primary/), and [`dbt_operations_shadow/`](dbt_operations_shadow/) are the five repository candidates in the controlled graph. Four are repaired; the shadow project is the candidate a reviewer rejected when one DataHub asset matched two implementations.

## Comparisons and earlier captures

These exist so the behaviour can be compared, not because they are the submission:

- [`ambiguity-resolution-review.html`](ambiguity-resolution-review.html) shows the transition from a blocked ambiguous mapping to an explicitly resolved campaign
- [`campaign-review.html`](campaign-review.html) compares contract-preserving against contract-propagating output on the same rename
- [`richer-graph-review.html`](richer-graph-review.html) compares both policies across the five-repository graph
- [`evaluation/report.html`](evaluation/report.html) is the 14-case adversarial corpus: 6 repaired, 8 refused

`generated/`, `generated-propagate/`, `generated-rich-preserve/`, `generated-rich-propagate/`, and `generated-rich-propagate-resolved/` are preserved captures from earlier runs, kept as labelled fixtures so the comparison pages stay reproducible. They are historical, not the submitted campaign.
