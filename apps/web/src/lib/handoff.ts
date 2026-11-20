import type { ConnectInviteStatus, Provider } from '../api/types'

/**
 * Who can connect each cloud, in one line (role pre-check, ADR-022). The role names are the
 * providers' own, checked against their documentation (sources in docs/adr/022).
 */
export const WHO_CAN: Record<Provider, string> = {
  aws: 'Needs an IAM user or role that can create IAM roles and CloudFormation stacks.',
  gcp: 'Needs someone who can turn on the billing export, share BigQuery datasets and grant IAM roles: Billing Account Administrator, or Project IAM Admin and BigQuery Admin.',
  azure:
    'Needs Owner, or Contributor plus User Access Administrator, on the subscription. Consent for the whole organization needs a Privileged Role Administrator.',
  digitalocean: 'Needs a team Owner, or a Member who can approve apps.',
}

/** What the cloud admin grants, in one phrase (the API's GRANTS say the same). */
export const GRANTS: Record<Provider, string> = {
  aws: 'a read-only IAM role that lets Cloudoptics read the bills, budgets, Savings Plans, resources and their metrics',
  gcp: 'read access to the billing export in BigQuery and a viewer role on your projects',
  azure: 'read access to the cost export in Blob Storage and the Reader role on the subscription or billing account',
  digitalocean: "read-only access to the team's invoices, balance and resources",
}

export const INVITE_STATUS_LABEL: Record<ConnectInviteStatus, string> = {
  sent: 'Sent',
  opened: 'Opened',
  connected: 'Connected',
  expired: 'Expired',
  revoked: 'Withdrawn',
}

/** Where the landing page keeps its session (this tab only), and the DigitalOcean return marker. */
export const INVITE_SESSION_KEY = 'cloudoptics.connect-invite'
/** The tenant ID the connect steps are given inside an invite session (the session knows its own). */
export const INVITE_TENANT = 'connect-invite'
