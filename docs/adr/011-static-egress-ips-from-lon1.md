# ADR-011: Static egress IPs from LON1

**Status:** Open — Sprint 0 verification

## Context

Customers may pin our role with `aws:SourceIp`, which needs fixed outbound IPs.

## Decision

Use the DO VPC NAT Gateway if it is available in LON1; otherwise an egress proxy on Droplets with reserved IPs for AWS-bound traffic.

## Consequences

Decided after the OPS-5 spike (docs/spikes/sprint-0.md).

## Alternatives considered

No IP pinning (weaker customer controls).

## References

docs/11a §6, Phase 9 O4
