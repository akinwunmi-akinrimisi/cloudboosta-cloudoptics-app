// Mirrors the API's camelCase JSON (docs/09 §3). Generated OpenAPI types replace these
// once the contract stabilises (ADR-005).
import type { CurrencyMode } from '../lib/currency'

export type Role = 'admin' | 'member' | 'viewer'

export interface Membership {
  tenantId: string
  tenantName: string
  role: Role
}

export interface Me {
  id: string
  email: string
  displayName: string | null
  preferredCurrencyView: CurrencyMode
  memberships: Membership[]
}

export interface Tenant {
  id: string
  name: string
  countryCode: string
  displayCurrency: string
  timeZone: string
  mfaRequired: boolean
  status: string
  createdAt: string
  role: Role
}

export type CheckStatus = 'ok' | 'warning' | 'error' | 'skipped'

export interface PermissionCheck {
  id: string
  label: string
  status: CheckStatus
  message: string
  hint: string | null
}

export type ConnectionStatus =
  | 'pending'
  | 'validating'
  | 'healthy'
  | 'degraded'
  | 'failed'
  | 'disconnected'

export interface Connection {
  id: string
  awsAccountId: string
  accountType: 'management' | 'standalone'
  roleArn: string
  status: ConnectionStatus
  createCurExport: boolean
  curBucket: string | null
  curPrefix: string | null
  curExportArn: string | null
  permissionChecks: PermissionCheck[]
  lastError: Record<string, string> | null
  lastValidatedAt: string | null
  createdAt: string
}

export interface ConnectionSetup {
  connection: Connection
  externalId: string
  /** Null until the templates are hosted in our S3 bucket (CloudFormation quick-create needs S3). */
  quickCreateUrl: string | null
  templateUrl: string
  templateDownloadUrl: string
  resourceTemplateUrl: string
  resourceTemplateDownloadUrl: string
  stackRegion: string
  principalArn: string
}

export interface ConnectionCreate {
  awsAccountId: string
  accountType: 'management' | 'standalone'
  createCurExport: boolean
}

export interface AuthConfig {
  mode: 'dev' | 'oidc'
  configured: boolean
  provider: 'workos' | 'oidc' | null
  issuer: string | null
  clientId: string | null
}

/** An external identity provider session (WorkOS AuthKit) the HTTP client can use. */
export interface AuthProvider {
  signedIn: boolean
  getToken(): Promise<string>
  signIn(): Promise<void>
  signOut(): void
}

/** cur: the Cost and Usage Report; cost_explorer: provisional figures until it arrives. */
export type CostSource = 'cur' | 'cost_explorer'

export interface CostSummary {
  /**
   * no_connection: nothing connected; waiting: connected, nothing loaded yet;
   * provisional: no Cost and Usage Report for the month yet, figures from Cost Explorer.
   */
  status: 'no_connection' | 'waiting' | 'provisional' | 'ready'
  source: CostSource | null
  /** The UTC month the totals cover, YYYY-MM. */
  month: string
  currency: 'USD'
  /** Last day (UTC) with cost data, YYYY-MM-DD. */
  dataThrough: string | null
  lastLoadAt: string | null
  lastLoadStatus: 'succeeded' | 'failed' | 'superseded' | null
  /** Amortised (effective) cost after credits, plus the invoiced (billed) view. */
  monthToDate: { effective: number; billed: number; credits: number } | null
  forecast: { amount: number; method: 'run_rate'; daysCovered: number; daysInMonth: number } | null
  previousMonth: number | null
}

export interface DailyCosts {
  source: CostSource | null
  currency: 'USD'
  /** Amortised cost after credits per UTC day, oldest first. */
  points: { date: string; amount: number }[]
}

export type CostDimension = 'service' | 'account' | 'region'

export interface CostBreakdown {
  source: CostSource | null
  month: string
  dimension: CostDimension
  currency: 'USD'
  /** This month's spend excluding tax. */
  total: number
  /** The largest items, then one "N more" item with key "other". */
  items: { key: string; label: string; amount: number; share: number }[]
}

export interface FxQuote {
  currency: string
  /** Local currency units per 1 USD. */
  rate: number
  rateDate: string
  basis: 'official' | 'market'
  /** manual: set by Cloudoptics staff; feed: reference feed (only when no staff rate exists). */
  source: 'manual' | 'feed'
  /** Carried forward from an earlier day, or from the feed. */
  estimated: boolean
}

export interface CurrentRate {
  currency: string
  basis: 'official' | 'market'
  /** Null when no usable rate exists: show USD only. */
  quote: FxQuote | null
}

export interface Api {
  readonly mode: 'http' | 'demo'
  authConfig(): Promise<AuthConfig>
  isSignedIn(): boolean
  signIn(email: string, name?: string): Promise<void>
  signOut(): void
  me(): Promise<Me>
  updateMe(view: CurrencyMode): Promise<Me>
  createTenant(body: { name: string; countryCode: string }): Promise<Tenant>
  getTenant(tenantId: string): Promise<Tenant>
  listConnections(tenantId: string): Promise<Connection[]>
  createConnection(tenantId: string, body: ConnectionCreate): Promise<ConnectionSetup>
  getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup>
  validateConnection(tenantId: string, connectionId: string): Promise<Connection>
  costSummary(tenantId: string): Promise<CostSummary>
  costDaily(tenantId: string): Promise<DailyCosts>
  costBreakdown(tenantId: string, dimension: CostDimension): Promise<CostBreakdown>
  fxCurrent(tenantId: string): Promise<CurrentRate>
}

export class ApiError extends Error {
  readonly status: number
  readonly fieldErrors: { field: string; message: string }[]

  constructor(status: number, title: string, fieldErrors: { field: string; message: string }[] = []) {
    super(title)
    this.status = status
    this.fieldErrors = fieldErrors
  }
}
