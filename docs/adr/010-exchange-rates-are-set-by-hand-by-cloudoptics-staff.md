# ADR-010: Exchange rates are set by hand by Cloudoptics staff

**Status:** Decided 22 Oct 2026

## Context

Official and market naira rates diverge, and no feed covers every African currency reliably. Customers need to trust the local figures.

## Decision

Staff set rates in the platform admin console. A commercial feed is shown for reference and used only for a currency that has never had a manual rate; such values are flagged as estimated. Changes over 5% warn, over 10% need a second admin, and a rate older than 3 working days raises an alert.

## Consequences

Local figures are auditable and match what finance teams expect. Someone must own daily updates (open: PRD Q3).

## Alternatives considered

Automatic feed only (wrong for parallel-market currencies); tenant-set rates (possible later).

## References

PRD FR-FX-01, -02, -06, -07
