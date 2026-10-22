# ADR-007: IAM Roles Anywhere with AWS Private CA; Secrets Manager and KMS

**Status:** Proposed — Sprint 0 spike

## Context

Workers on DigitalOcean must assume customer roles without long-lived AWS keys (threat T2).

## Decision

A broker AWS account. Workloads get short-lived X.509 certificates from AWS Private CA (short-lived certificate mode) and exchange them through IAM Roles Anywhere for ≤ 1 h credentials, then call `sts:AssumeRole` with the tenant's External ID. Secrets sit in AWS Secrets Manager (`eu-west-2`), synced by External Secrets Operator; envelope encryption with KMS.

## Consequences

No static keys on DigitalOcean. If the spike fails, fall back to an IAM user with keys rotated every ≤ 90 days and tight IP conditions.

## Alternatives considered

IAM user with rotated keys; a small AWS-hosted assume-role broker.

## References

PRD FR-CON-05, docs/10, docs/spikes/sprint-0.md (CC-1)
