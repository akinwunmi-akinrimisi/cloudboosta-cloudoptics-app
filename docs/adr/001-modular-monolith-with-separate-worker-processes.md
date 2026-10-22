# ADR-001: Modular monolith with separate worker processes

**Status:** Accepted

## Context

A team of about 8 must ship an MVP in 15 weeks. The domain has clear areas (connectors, ingestion, analysis, recommendations, FX, budgets) but low request volume at launch. Microservices would add network, deployment and data-consistency costs before they pay off.

## Decision

One Python codebase with strict module boundaries (docs/09 §1.1): each module owns its tables and exposes an internal interface, with no cross-module table access. It runs as several processes from the same image: `api`, `worker`, `scheduler` and `renderer`.

## Consequences

Simple deploys and refactors, and one transaction scope for transactional job enqueue. Module boundaries must be enforced in review and with import-linter rules (Sprint 1). A module can be split into a service later if its scaling profile diverges.

## Alternatives considered

Microservices per domain (too much operational overhead for the team size); a single process (cannot scale ingestion separately from the API).

## References

docs/09 §1.1, Phase 9 D1
