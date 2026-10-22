# ADR-008: DOKS, Helm, Argo CD and Terraform

**Status:** Proposed

## Context

The stakeholder chose DigitalOcean. Workers need queue-depth autoscaling and controlled egress.

## Decision

DOKS (LON1 primary, AMS3 secondary) with `general` and `workers` node pools, KEDA on queue depth, Helm charts deployed by Argo CD, Terraform 1.x (OpenTofu-compatible) for DO, AWS, Cloudflare and GitHub. Kyverno admission policies; cosign keyless signing.

## Consequences

Auditable GitOps deploys with easy rollback. Needs Kubernetes skills; the fallback is DO App Platform.

## Alternatives considered

DO App Platform (no KEDA, weaker egress control); Pulumi or CDK.

## References

docs/11a §6
