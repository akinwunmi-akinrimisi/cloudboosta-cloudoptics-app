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

export type ConnectionStatus = 'pending' | 'validating' | 'healthy' | 'degraded' | 'failed' | 'disconnected'

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
  /** One-click CloudFormation link for the resource read-only stack; null like quickCreateUrl. */
  resourceQuickCreateUrl: string | null
  /** Management connections: one stack that rolls the role out to every member account (StackSet). */
  resourceStacksetQuickCreateUrl: string | null
  resourceStacksetDownloadUrl: string
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
  forecast: {
    amount: number
    method: 'run_rate'
    daysCovered: number
    daysInMonth: number
  } | null
  previousMonth: number | null
  /** The latest check of this or last month's totals against AWS Cost Explorer. */
  reconciliation?: {
    status: 'ok' | 'mismatch' | 'failed'
    month: string
    differencePct: number | null
    accountsMismatched: number
    checkedAt: string
  } | null
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

export interface ResourceScan {
  status: 'queued' | 'succeeded' | 'partial' | 'skipped'
  /** The role check done at once, per account; `error` is the AWS error code. */
  accounts: { awsAccountId: string; access: boolean; error: string | null }[]
}

export interface CloudAccount {
  connectionId: string
  awsAccountId: string
  name: string | null
  accountType: 'management' | 'member' | 'standalone'
  /** Status in the AWS Organization (ACTIVE, SUSPENDED, NOT_LISTED…); null if never discovered. */
  orgStatus: string | null
  orgSeenAt: string | null
  /** The resource read-only role is deployed in this account. */
  resourceAccess: boolean
  /** The Cost and Usage Report has costs for it this month or last. */
  hasCostData: boolean
}

export interface AccountList {
  /** Counts accounts still in the organization. */
  coverage: {
    accounts: number
    withCostData: number
    withResourceAccess: number
  }
  accounts: CloudAccount[]
}

export type RecommendationStatus =
  | 'new'
  | 'assigned'
  | 'in_progress'
  | 'done'
  | 'verified'
  | 'dismissed'
  | 'snoozed'
  | 'resolved_externally'
  | 'expired'

export interface RecommendationFix {
  /** AWS CLI commands, run in order. */
  steps: string[]
  rollback: string[]
  notes?: string
  alternative?: { title: string; monthlyCostUsd: number; steps: string[] }
}

export interface Recommendation {
  id: string
  type: string
  ruleId: string
  accountId: string
  regionId: string
  resourceId: string
  title: string
  currentConfig: Record<string, unknown>
  recommendedConfig: Record<string, unknown>
  costNowUsdMonth: number
  costAfterUsdMonth: number
  savingsUsdMonth: number
  /** effective_rate: the workspace's own AWS rate; list_price: AWS on-demand list price. */
  savingsBasis: 'effective_rate' | 'list_price'
  effort: 'S' | 'M' | 'L'
  reversibility: 'reversible' | 'with_snapshot' | 'irreversible'
  restartRequired: boolean
  riskScore: number
  riskLevel: 'low' | 'medium' | 'high'
  riskFactors: { factor: string; points: number }[]
  confidence: 'high' | 'medium' | 'low'
  confidenceNotes: string[]
  priorityScore: number
  evidence: { kind: string; value: unknown; [key: string]: unknown }[]
  fix: RecommendationFix
  /** Set on an alternative to another recommendation for the same resource. */
  primaryOf: string | null
  status: RecommendationStatus
  firstDetectedAt: string
  lastEvaluatedAt: string
}

export interface RecommendationSummary {
  openCount: number
  savingsUsdMonth: number
  byRule: { ruleId: string; count: number; savingsUsdMonth: number }[]
  lastEvaluatedAt: string | null
}

export interface RecommendationQuery {
  status?: 'open' | 'all'
  alternatives?: boolean
}

export type StatusChange = {
  status: 'new' | 'in_progress' | 'done' | 'dismissed'
  reason?: string
}

export interface AuditEntry {
  id: number
  occurredAt: string
  actor: { type: 'user' | 'system' | 'staff'; email: string | null; name: string | null }
  action: string
  targetType: string | null
  targetId: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  ip: string | null
}

export interface AuditPage {
  entries: AuditEntry[]
  /** Pass as ``before`` for the next (older) page; null on the last page. */
  nextBefore: number | null
  /** False if any entry was altered or removed (the hash chain does not verify). */
  verified: boolean
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
  disconnectConnection(tenantId: string, connectionId: string): Promise<void>
  /** Look for the resource role now. `queued` means the worker scans within a minute or two. */
  scanResources(tenantId: string, connectionId: string): Promise<ResourceScan>
  costSummary(tenantId: string): Promise<CostSummary>
  costDaily(tenantId: string, days?: number): Promise<DailyCosts>
  costBreakdown(tenantId: string, dimension: CostDimension, limit?: number): Promise<CostBreakdown>
  fxCurrent(tenantId: string): Promise<CurrentRate>
  listAccounts(tenantId: string): Promise<AccountList>
  listRecommendations(tenantId: string, query?: RecommendationQuery): Promise<Recommendation[]>
  recommendationSummary(tenantId: string): Promise<RecommendationSummary>
  getRecommendation(tenantId: string, id: string): Promise<Recommendation>
  updateRecommendation(tenantId: string, id: string, change: StatusChange): Promise<Recommendation>
  auditLog(tenantId: string, before?: number): Promise<AuditPage>
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
