# ADR-003: Procrastinate (PostgreSQL-backed) job queue

**Status:** Proposed

## Context

Ingestion and analysis run as thousands of small per-tenant jobs that must be serialised per tenant and enqueued in the same transaction as the data change.

## Decision

Procrastinate 3.x behind a thin internal `jobs` module (`enqueue`, `schedule`, decorators). Per-tenant queueing locks such as `tenant:{id}:cur`; periodic tasks replace a separate cron.

## Consequences

No extra stateful service and jobs are visible in SQL. Throughput is lower than Redis queues, which is enough for the MVP. Reassess Temporal at v2 for multi-step remediation with approvals.

## Alternatives considered

Celery + Valkey (no transactional enqueue, weaker locking); Dramatiq; Temporal now (heavier to operate).

## References

docs/11a §2.2
