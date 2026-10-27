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
  /** ``cloudoptics-access`` once on the single stack; the older billing stack's name before. */
  stackName: string
  /** The single stack's version as last reported; null while on the older stacks. */
  stackVersion: string | null
}

export const ACCESS_STACK_NAME = 'cloudoptics-access'

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
  /** One-click link to the single stack for this connection (move or set up again). */
  accessQuickCreateUrl: string | null
  accessTemplateUrl: string
  accessTemplateDownloadUrl: string
  callbackUrl: string
  connectionRef: string
  latestStackVersion: string
  /** True while on the older stacks, or when the single stack is older than the latest. */
  updateAvailable: boolean
  /** The stack's page in the AWS console (Update and Delete are there). */
  stackConsoleUrl: string
  /** AWS's Update stack page with the latest template already chosen. */
  stackUpdateUrl: string
}

/** A one-click link made before the AWS account is known; the stack reports the account. */
export interface ConnectionIntent {
  id: string
  status: 'waiting' | 'connected'
  connectionId: string | null
  /** Why the stack's report was refused (for example, the account is already connected). */
  error: string | null
}

export interface IntentSetup {
  intent: ConnectionIntent
  /** Null when quick-create links are off: set the stack up by hand with the values below. */
  quickCreateUrl: string | null
  templateUrl: string
  templateDownloadUrl: string
  externalId: string
  principalArn: string
  resourceTemplateUrl: string
  callbackUrl: string
  connectionRef: string
  stackRegion: string
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
  /**
   * The role check done at once, per account; `error` is the AWS error code. `rollout` says,
   * in plain words, how AWS's rollout of the role to a member account without access is going.
   */
  accounts: { awsAccountId: string; access: boolean; error: string | null; rollout?: string | null }[]
}

/** How far a running scan has got. The total is an estimate until every account's regions are known. */
export interface ScanProgress {
  regionsDone: number
  regionsTotal: number
}

/** A connection's latest resource scan. */
export interface LastScan {
  connectionId: string
  /**
   * `running`: still scanning, or queued for the scanner (`progress` is null until it starts).
   * `partial`: some accounts or regions could not be read (see `errors`).
   */
  status: 'running' | 'succeeded' | 'partial' | 'failed'
  startedAt: string
  finishedAt: string | null
  resources: number
  accounts: number
  accountsWithAccess: number
  regions: number
  /** "<account> <region>: <AWS error code>" for each part that could not be read. */
  errors: string[]
  progress?: ScanProgress | null
}

export interface InventorySummary {
  lastScanAt: string | null
  counts: { resourceType: string; count: number }[]
  scans: LastScan[]
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
  /** Plain-language steps when the fix is not a few commands (for example a move to Arm). */
  checklist?: string[]
  alternative?: { title: string; monthlyCostUsd?: number; steps: string[] }
  /** The resource's page in the AWS console and the clicks there; shown first. */
  console?: { url: string; steps: string[] }
}

export interface UsageSeries {
  metric: string
  days: { day: string; average: number | null; maximum: number | null }[]
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
  /** The member it is assigned to, if any. */
  assigneeId: string | null
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

/** A status change, an assignment (null unassigns) or both. */
export type RecommendationChange = {
  status?: 'new' | 'in_progress' | 'done' | 'dismissed'
  reason?: string
  assigneeId?: string | null
}
export type StatusChange = RecommendationChange

export interface RecommendationEvent {
  id: string
  kind: 'status_changed' | 'assigned' | 'commented'
  actorId: string | null
  actorName: string | null
  fromStatus: RecommendationStatus | null
  toStatus: RecommendationStatus | null
  assigneeId: string | null
  assigneeName: string | null
  body: string | null
  createdAt: string
}

export interface Member {
  userId: string
  email: string
  displayName: string | null
  role: Role
  joinedAt: string
}

/** `no_rate`: a local-currency budget with no usable exchange rate yet; `no_data`: no bills this month. */
export type BudgetStatus = 'on_track' | 'at_risk' | 'over' | 'no_data' | 'no_rate'

export interface BudgetScope {
  type: 'tenant' | 'accounts'
  accounts: string[]
}

export interface BudgetThresholdInput {
  kind: 'actual' | 'forecast'
  percent: number
}

export interface BudgetInput {
  name: string
  scope: BudgetScope
  amount: number
  currency: string
  thresholds: BudgetThresholdInput[]
  recipients: { users: string[]; emails: string[] }
}

/** A monthly budget and how this month is going: amounts in its currency, with the USD they come from. */
export interface Budget extends BudgetInput {
  id: string
  isActive: boolean
  createdAt: string
  status: BudgetStatus
  period: string
  dataThrough: string | null
  /** Budget currency per USD; null when there is no usable rate. */
  rate: number | null
  amountUsd: number | null
  actual: number | null
  actualUsd: number
  forecast: number | null
  forecastUsd: number | null
  actualPercent: number | null
  forecastPercent: number | null
  thresholds: (BudgetThresholdInput & { crossed: boolean; alerted: boolean })[]
}

export type AnomalyStatus = 'open' | 'investigating' | 'expected' | 'resolved'

export interface AnomalyContributor {
  usageType: string
  region: string | null
  actualUsd: number
  expectedUsd: number
  impactUsd: number
}

/** A day's spend for one account and service well above its same-weekday baseline (USD). */
export interface Anomaly {
  id: string
  date: string
  accountId: string
  accountName: string
  serviceCode: string
  serviceName: string
  actualUsd: number
  expectedUsd: number
  impactUsd: number
  /** Null when nothing was expected (new spend). */
  deviationPercent: number | null
  contributors: AnomalyContributor[]
  status: AnomalyStatus
  /** Stored as expected because the same scope was marked expected in the week before. */
  suppressed: boolean
  note: string | null
  statusChangedAt: string | null
  statusChangedBy: string | null
  detectedAt: string
  alerted: boolean
}

export interface AnomalyDetail extends Anomaly {
  /** Four weeks before the anomaly and up to a week after; expected/upper null without history. */
  series: { date: string; actual: number; expected: number | null; upper: number | null }[]
}

export interface AlertSettings {
  anomalyMinImpact: number
  anomalyMinImpactCurrency: string
  anomalyMinPercent: number
  anomalyEmails: string[]
}

export interface AlertSettingsView {
  settings: AlertSettings
  /** The minimum in USD used now; null when the workspace currency has no rate (USD default applies). */
  minImpactUsd: number | null
  defaults: AlertSettings
}

/** Emails a member can turn off. */
export interface NotificationChoices {
  weeklyDigest: boolean
  anomalyAlert: boolean
  monthlyReport: boolean
}

/** A month's executive report: stored figures do not change once made. Money in USD, local where a rate exists. */
export interface MonthlyReport {
  month: string
  workspace: string
  currency: string
  generatedAt: string
  spend: {
    usd: number
    local: number | null
    /** Month average, local per USD. */
    rate: number | null
    previousUsd: number
    previousLocal: number | null
    previousRate: number | null
    changeUsd: number
    changePercent: number | null
    /** Local change split into usage and exchange-rate effects. */
    usageEffect: number | null
    fxEffect: number | null
    /** Credits applied (zero or negative). */
    creditsUsd: number
  }
  drivers: { serviceName: string; usd: number; previousUsd: number; changeUsd: number }[]
  budgets: { name: string; currency: string; amount: number; status: string; actual: number | null; actualPercent: number | null }[]
  savings: { openCount: number; openUsdMonth: number; newCount: number; doneCount: number; doneUsdMonth: number }
  topRecommendations: { title: string; savingsUsdMonth: number; riskLevel: string; effort: string }[]
  anomalies: { count: number; impactUsd: number }
  accuracy: {
    dataThrough: string
    reconciliation: 'ok' | 'mismatch' | 'failed' | null
    differencePercent: number | null
    checkedAt: string | null
    note: string
  }
}

export type CreditsStatus = 'no_credits' | 'no_balance' | 'ok' | 'ending_soon' | 'used_up'

/** AWS credits runway in USD: burn is the 30-day average; remaining counts down from the entered balance. */
export interface CreditsRunway {
  status: CreditsStatus
  dataThrough: string | null
  appliedMonthUsd: number
  burnUsdDay: number
  burnDays: number
  /** What the bill becomes once credits run out (30-day month). */
  spendBeforeCreditsUsdMonth: number
  balance: { amountUsd: number; asOf: string; expiresOn: string | null; note: string | null; enteredBy: string | null; enteredAt: string } | null
  usedSinceBalanceUsd: number | null
  remainingUsd: number | null
  daysLeft: number | null
  depletionDate: string | null
  /** The credits expire before they would run out. */
  expiresFirst: boolean
  daily: { date: string; amount: number }[]
}

export interface CreditBalanceInput {
  amountUsd: number
  asOf?: string
  expiresOn?: string | null
  note?: string | null
}

export interface ReportList {
  reports: { month: string; generatedAt: string; spendUsd: number; spendLocal: number | null; currency: string }[]
  /** The last full month, shown as a draft until its report is made. */
  latestMonth: string
}

export interface NotificationPreferences {
  /** What this member gets: their own choice, else the workspace default. */
  mine: NotificationChoices
  defaults: NotificationChoices
}

export interface DigestPreview {
  weekStart: string
  weekEnd: string
  subject: string
  text: string
  html: string
}

export interface Invite {
  id: string
  email: string
  role: Role
  invitedByEmail: string | null
  createdAt: string
  expiresAt: string
  status: 'pending' | 'expired'
  /** Whether the invite email went out; null when listed later. */
  emailSent: boolean | null
}

export interface RuleSettings {
  disabledRules: string[]
  /** Resources with this tag key are left out of every rule. */
  ignoreTag: string
  idleCpuPercent: number
  idleDays: number
}

export interface RuleSettingsView {
  rules: string[]
  settings: RuleSettings
}

export interface TenantChange {
  displayCurrency?: string
  timeZone?: string
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
  updateTenant(tenantId: string, change: TenantChange): Promise<Tenant>
  listMembers(tenantId: string): Promise<Member[]>
  listBudgets(tenantId: string): Promise<Budget[]>
  /** Members and admins. */
  createBudget(tenantId: string, body: BudgetInput): Promise<Budget>
  updateBudget(tenantId: string, id: string, change: Partial<BudgetInput> & { isActive?: boolean }): Promise<Budget>
  deleteBudget(tenantId: string, id: string): Promise<void>
  listAnomalies(tenantId: string, status?: AnomalyStatus): Promise<Anomaly[]>
  getAnomaly(tenantId: string, id: string): Promise<AnomalyDetail>
  /** Members and admins. Expected stops new anomalies for the same scope for a week. */
  updateAnomaly(tenantId: string, id: string, change: { status: AnomalyStatus; note?: string | null }): Promise<AnomalyDetail>
  getNotificationPreferences(tenantId: string): Promise<NotificationPreferences>
  saveMyNotifications(tenantId: string, change: Partial<NotificationChoices>): Promise<NotificationPreferences>
  /** Admins only: for members who have not chosen. */
  saveNotificationDefaults(tenantId: string, change: Partial<NotificationChoices>): Promise<NotificationPreferences>
  /** Last full week's digest as emailed; 404 when there are no bills for it. */
  digestPreview(tenantId: string): Promise<DigestPreview>
  getCredits(tenantId: string): Promise<CreditsRunway>
  /** Admins only: the balance from the AWS Billing console (Credits page). */
  setCreditBalance(tenantId: string, body: CreditBalanceInput): Promise<CreditsRunway>
  listReports(tenantId: string): Promise<ReportList>
  /** `stored: false`: worked out from today's data (made on the 3rd working day). 404 without bills. */
  getReport(tenantId: string, month: string): Promise<{ report: MonthlyReport; stored: boolean }>
  /** Admins only: make it again from today's data and email every recipient. */
  sendReport(tenantId: string, month: string): Promise<{ sentTo: number; report: MonthlyReport }>
  getReportSettings(tenantId: string): Promise<{ emails: string[] }>
  saveReportSettings(tenantId: string, emails: string[]): Promise<{ emails: string[] }>
  getAlertSettings(tenantId: string): Promise<AlertSettingsView>
  /** Admins only. */
  saveAlertSettings(tenantId: string, settings: AlertSettings): Promise<AlertSettingsView>
  /** Admins only. The invitee joins the first time they sign in with that email. */
  inviteMember(tenantId: string, email: string, role: Role): Promise<Invite>
  listInvites(tenantId: string): Promise<Invite[]>
  revokeInvite(tenantId: string, inviteId: string): Promise<void>
  changeRole(tenantId: string, userId: string, role: Role): Promise<void>
  removeMember(tenantId: string, userId: string): Promise<void>
  listConnections(tenantId: string): Promise<Connection[]>
  createConnection(tenantId: string, body: ConnectionCreate): Promise<ConnectionSetup>
  createIntent(tenantId: string): Promise<IntentSetup>
  getIntent(tenantId: string, intentId: string): Promise<ConnectionIntent>
  getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup>
  validateConnection(tenantId: string, connectionId: string): Promise<Connection>
  disconnectConnection(tenantId: string, connectionId: string): Promise<void>
  /** Look for the resource role now. `queued` means the worker scans within a minute or two. */
  scanResources(tenantId: string, connectionId: string): Promise<ResourceScan>
  /** What the scans found, and each connection's latest scan. */
  resourceSummary(tenantId: string): Promise<InventorySummary>
  costSummary(tenantId: string): Promise<CostSummary>
  costDaily(tenantId: string, days?: number): Promise<DailyCosts>
  costBreakdown(tenantId: string, dimension: CostDimension, limit?: number): Promise<CostBreakdown>
  fxCurrent(tenantId: string): Promise<CurrentRate>
  listAccounts(tenantId: string): Promise<AccountList>
  listRecommendations(tenantId: string, query?: RecommendationQuery): Promise<Recommendation[]>
  recommendationSummary(tenantId: string): Promise<RecommendationSummary>
  getRecommendation(tenantId: string, id: string): Promise<Recommendation>
  updateRecommendation(tenantId: string, id: string, change: RecommendationChange): Promise<Recommendation>
  updateRecommendations(tenantId: string, ids: string[], change: RecommendationChange): Promise<{ updated: number }>
  recommendationEvents(tenantId: string, id: string): Promise<RecommendationEvent[]>
  recommendationUsage(tenantId: string, id: string): Promise<UsageSeries[]>
  addComment(tenantId: string, id: string, body: string): Promise<RecommendationEvent[]>
  getRuleSettings(tenantId: string): Promise<RuleSettingsView>
  /** Saving re-runs the rules, so recommendations change straight away. */
  saveRuleSettings(tenantId: string, settings: RuleSettings): Promise<RuleSettingsView>
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
