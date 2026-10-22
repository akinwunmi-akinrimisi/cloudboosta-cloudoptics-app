# ADR-004: PostgreSQL, Spaces/Parquet and DuckDB; ClickHouse at a scale trigger

**Status:** Proposed

## Context

Cost data is append-heavy and analytical; tenancy needs strong isolation; the team cannot run a data warehouse at launch.

## Decision

DO Managed PostgreSQL 16+ with row-level security for operational data and daily aggregates; normalised FOCUS-aligned Parquet on DO Spaces; DuckDB embedded in workers for normalisation and bounded drill-downs. Move to ClickHouse behind `CostQueryService` when `cost_daily` passes about 1B rows or p95 query time passes 2 s.

## Consequences

One managed database also hosts the job queue. DuckDB is single-process, so concurrency comes from pre-aggregating into Postgres.

## Alternatives considered

BigQuery or Snowflake (another cloud, egress cost); TimescaleDB (extension availability on DO to confirm).

## References

docs/09 §2, docs/11a §2.3 and §4
