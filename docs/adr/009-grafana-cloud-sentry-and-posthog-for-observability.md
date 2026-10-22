# ADR-009: Grafana Cloud, Sentry and PostHog for observability

**Status:** Proposed

## Context

A small team needs logs, metrics, traces, error tracking and product analytics without running them.

## Decision

OpenTelemetry instrumentation; Grafana Cloud (Loki, Mimir, Tempo, alerting); Sentry with PII scrubbing; PostHog EU for product events and feature flags behind an OpenFeature interface. No full session replay (privacy posture, docs/12 §5).

## Consequences

Managed and vendor-neutral at the instrumentation layer. Costs grow with volume; sampling is set per environment.

## Alternatives considered

Self-hosted kube-prometheus-stack and Loki (cheaper at scale, more operations).

## References

docs/11a §7
