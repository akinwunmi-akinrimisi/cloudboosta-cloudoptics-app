"""Raw billing file landing, normalisation to FOCUS, data quality checks, aggregates
and reconciliation.

Owns its own tables and exposes an internal interface; no cross-module table access
(docs/09 section 1.1).

Pipeline: ``source`` (find manifests, download files) -> ``focus`` (CUR 2.0 to FOCUS)
-> ``quality`` (block/warn checks) -> ``aggregate`` (daily totals) -> ``service`` (atomic
replace in ``cost_daily``). ``queries`` is the read side for the API; ``jobs`` schedules it.
"""
