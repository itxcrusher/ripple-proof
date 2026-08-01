# RippleProof Change Impact Report

> Proposed change only. RippleProof does not execute this SQL. A human must review the migration and every tracked consumer.

- Source asset: `urn:li:dataset:(urn:li:dataPlatform:postgres,analytics.orders,PROD)`
- Request: `rename user_id to customer_id`
- Generator: `deterministic parser`

## Proposed SQL

```sql
ALTER TABLE "analytics"."orders" RENAME COLUMN "user_id" TO "customer_id";
```

## DataHub impact analysis

DataHub found 5 downstream asset(s) for `user_id` (4 column-level, 1 asset-level):
  - DATASET `customer_identity_export` (dbt, 2 hops away; column-level; derived column(s): user_id)
  - DATASET `customer_revenue` (dbt, 2 hops away; column-level; derived column(s): user_id)
  - DATASET `customer_segments` (dbt, 2 hops away; column-level; derived column(s): user_id)
  - DATASET `orders_daily` (dbt, 1 hop away; column-level; derived column(s): user_id)
  - DASHBOARD `Revenue Dashboard` (looker, 2 hops away; asset-level reachability; column usage unconfirmed)

Remediation plan:
  - `customer_identity_export`: Treat this as a transitive consumer. Update it only when the chosen migration propagates `customer_id` through the preceding model; leave it unchanged when a one-hop compatibility boundary preserves `user_id`. DataHub reports derived column(s) `user_id`. Route review to Operations Data.
  - `customer_revenue`: Treat this as a transitive consumer. Update it only when the chosen migration propagates `customer_id` through the preceding model; leave it unchanged when a one-hop compatibility boundary preserves `user_id`. DataHub reports derived column(s) `user_id`. Route review to Finance Analytics.
  - `customer_segments`: Treat this as a transitive consumer. Update it only when the chosen migration propagates `customer_id` through the preceding model; leave it unchanged when a one-hop compatibility boundary preserves `user_id`. DataHub reports derived column(s) `user_id`. Route review to Growth Analytics.
  - `orders_daily`: Update the upstream reference from `user_id` to `customer_id`. Confirm whether downstream column(s) `user_id` should keep their current names or be renamed too. Route review to Analytics Engineering.
  - `Revenue Dashboard`: Inspect this asset for references to `user_id` before applying the change. DataHub confirms reachability, but column usage is unconfirmed. Route review to Revenue Operations.

Review and update each tracked consumer before merging. This report cannot detect dependencies missing from DataHub.

## Generated downstream artifacts

Proposed downstream patches:
- `dbt_operations_primary/models/customer_identity_primary.sql` for `customer_identity_export` (2 executable reference(s) updated, 0 output alias(es) preserved; discovered via dbt-manifest)
- `dbt_finance_project/models/customer_revenue.sql` for `customer_revenue` (2 executable reference(s) updated, 0 output alias(es) preserved; discovered via dbt-manifest)
- `dbt_growth_project/models/customer_segments.sql` for `customer_segments` (2 executable reference(s) updated, 0 output alias(es) preserved; discovered via dbt-manifest)
- `dbt_project/models/orders_daily.sql` for `orders_daily` (3 executable reference(s) updated, 0 output alias(es) preserved; discovered via dbt-manifest)

Updated dbt schema contracts:
- `dbt_operations_primary/models/schema.yml` for `customer_identity_export`
- `dbt_finance_project/models/schema.yml` for `customer_revenue`
- `dbt_growth_project/models/schema.yml` for `customer_segments`
- `dbt_project/models/schema.yml` for `orders_daily`

Assets requiring manual review:
- `Revenue Dashboard`: DataHub reports asset-level reachability but not column usage.

Explicit model mapping resolutions:
- `customer_identity_export`: selected `dbt_operations_primary/models/customer_identity_primary.sql`; retained unselected candidate(s): `dbt_operations_shadow/models/customer_identity_shadow.sql`

Validation gates:
- PASSED `patch-integrity`: Every generated SQL and dbt contract diff is non-empty and its source/proposed hashes match.
- PASSED `output-contract`: Every patched model propagates the new output name without a legacy alias; 4 declared dbt contract(s) were updated with it.
- PASSED `lineage-accounting`: All 5 DataHub lineage asset(s) are patched, protected, or assigned to manual review.
- WARNING `manual-review`: 1 asset(s) still require human investigation.
- PASSED `review-routing`: DataHub ownership assigns reviewers to the source and all 5 downstream asset(s).
- PASSED `model-mapping`: 1 ambiguous DataHub asset mapping(s) were explicitly resolved and hash-recorded.
- PASSED `dbt-build`: dbt 1.9.10 built and tested the proposed campaign with DuckDB across 4 validation root(s).
- PASSED `context-fingerprint`: Canonical DataHub schema, lineage, and ownership context was captured as SHA-256 cc60e67bd6b36142926815e34d6c3c238f88beaeea20ebcc973a11fbc47c5ab6.

Executable validation: PASSED with dbt 1.9.10 and DuckDB in disposable copied repositories.

Patch mode: propagate the rename through every confirmed dbt consumer. This changes downstream output contracts.
The target repositories were not modified. Proposed files and diffs require human review.

---

Generated by RippleProof from DataHub metadata. Missing lineage may mean untracked dependencies, not a safe change.
