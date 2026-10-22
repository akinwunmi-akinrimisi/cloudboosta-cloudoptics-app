# Sprint 0 spikes and verifications

These need real cloud accounts, so they are run by the team, not in CI. Each spike ends with a result recorded here and its ADR moved to Accepted or changed.

**Prerequisites:** DigitalOcean team account; AWS broker account and two test AWS accounts (one payer, one member) with a CUR 2.0 export and resources in `af-south-1`; Cloudflare zone; WorkOS and Keycloak sandboxes.

| ID | Question | Owner | Timebox | ADR |
|---|---|---|---|---|
| CC-1 | Can DOKS workloads get AWS credentials with no long-lived keys? | Tech Lead + DevOps | 2 days | ADR-007 |
| ADM-1 | WorkOS AuthKit or Keycloak? | Full-stack | 1.5 days | ADR-006 |
| OPS-5 | Can we get static egress IPs from LON1? | DevOps | 1 day | ADR-011 |
| IN-1v | Does the synthetic CUR fixture match a real export? | Backend | 0.5 day | — |
| V-1 | EBS modification cooldown and Compute Optimizer automation scope in `af-south-1` | Backend | 0.5 day | — (docs/12 §8) |

---

## CC-1: IAM Roles Anywhere + AWS Private CA from DOKS

**Steps**
1. In the broker account (`eu-west-2`), create an AWS Private CA in short-lived certificate mode and a Roles Anywhere trust anchor pointing at it.
2. Create a Roles Anywhere profile and a broker role whose only permission is `sts:AssumeRole` on `arn:aws:iam::*:role/CloudopticsReadOnly*`, with a condition on the certificate subject (`aws:PrincipalTag/x509Subject/CN`).
3. On DOKS, run a pod with a sidecar that requests a certificate (via a small issuer service using `acm-pca:IssueCertificate`, itself authenticated by a bootstrap path to be defined) and refreshes it before expiry; the app uses `aws_signing_helper credential-process`.
4. From the pod, assume the role in a test customer account with an External ID; call `ce:GetCostAndUsage` and `ec2:DescribeVolumes` in `af-south-1`.
5. Revoke or let the certificate expire and confirm calls fail within the credential lifetime.

**Pass criteria:** no static AWS keys anywhere in the cluster or CI; credentials ≤ 1 h; renewal survives pod restarts; CloudTrail shows the broker session name `cloudoptics-<tenant>-<job>`; monthly CA cost recorded.

**Watch for:** how the issuer bootstraps its own identity (this is the hard part; if it needs a static key, document the blast radius and rotation, or use a hardware-backed key in a DO-managed secret). **Fallback:** IAM user with keys rotated every ≤ 90 days, restricted by `aws:SourceIp` to our egress IPs (OPS-5).

**Result:** _pending_

---

## ADM-1: WorkOS AuthKit vs Keycloak

**Build the same flow in both:** sign-up with email verification, Google login, TOTP MFA enforcement, invite into a tenant, role claim mapped to our `membership` table, session refresh, logout everywhere, and a SAML SSO test connection.

**Score (1–5):** integration effort, hosted UI branding (Cloudoptic tokens), MFA and SSO coverage, SCIM, data location, cost at 50 tenants / 1,000 users / 10 SSO connections, operational load, exit path (we store only `auth_subject`).

**Result:** _pending_

---

## OPS-5: static egress from LON1

1. Check whether DO VPC NAT Gateway is available in LON1 and works with DOKS node pools.
2. If not, deploy an egress proxy (two Droplets with reserved IPs, Squid or Envoy, health-checked) and route only AWS API hostnames through it.
3. Confirm that AWS sees only the reserved IPs (CloudTrail `sourceIPAddress`) and measure added latency to `sts`, `ce` (us-east-1) and `af-south-1` endpoints.

**Pass criteria:** stable IPs we can publish to customers for `aws:SourceIp` conditions; failover within 60 s; cost recorded.

**Result:** _pending_

---

## IN-1v: fixture vs real CUR 2.0 export

Compare `tools/cur-fixture` output with one month of a real BCM Data Exports CUR 2.0 file from the test payer:

- column names and types (especially rates as strings, `product` and `resource_tags` maps, timestamp precision);
- manifest field names and folder layout;
- usage-type strings for public IPv4 idle addresses, NAT, Local Zones (`af-south-1-los-1a`) and RDS Extended Support (marked "verify" in the generator);
- Savings Plan covered usage, negation and recurring fee rows.

Update `cur_fixture/schema.py` and `generator.py` to match, then remove the "verify" notes.

**Result:** _pending_
