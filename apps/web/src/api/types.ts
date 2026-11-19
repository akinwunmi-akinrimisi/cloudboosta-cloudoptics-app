// Mirrors the API's camelCase JSON (docs/09 §3). Generated OpenAPI types replace these
// once the contract stabilises (ADR-005).
import type { CurrencyMode } from '../lib/currency'

export type Role = 'admin' | 'member' | 'viewer'

/** A Cloudoptics staff member's active break-glass grant: read-only until `expiresAt`. */
export interface SupportSession {
  grantId: string
  expiresAt: string
}

export interface Membership {
  tenantId: string
  tenantName: string
  role: Role
  /** Set for a workspace staff reads under support access (role viewer). */
  support?: SupportSession | null
}

export interface Me {
  id: string
  email: string
  displayName: string | null
  preferredCurrencyView: CurrencyMode
  memberships: Membership[]
  /** Cloudoptics staff (platform admins): the support access tool is shown. */
  staff?: boolean
}

export type SupportGrantStatus = 'active' | 'ended' | 'expired'

/** Staff break-glass access to one workspace (internal tool, /staff). */
export interface SupportGrant {
  id: string
  tenantId: string
  tenantName: string
  staffEmail: string
  staffName: string | null
  justification: string
  ticketRef: string
  createdAt: string
  expiresAt: string
  endedAt: string | null
  endedByEmail: string | null
  status: SupportGrantStatus
  /** Requests made under the grant (each one is recorded). */
  requests: number
}

export interface SupportGrantRequest {
  tenantId: string
  justification: string
  /** The support ticket (required, docs/11b §7.4). */
  ticketRef: string
  /** 15 to 120 minutes; 60 by default. */
  durationMinutes: number
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

/** The cloud a connection, account or finding belongs to. Only AWS is live today. */
export type Provider = 'aws' | 'gcp' | 'azure' | 'digitalocean'

export type ConnectionStatus = 'pending' | 'validating' | 'healthy' | 'degraded' | 'failed' | 'disconnected'

export interface Connection {
  id: string
  awsAccountId: string
  /** The cloud provider; absent from older payloads, which are all AWS. */
  provider?: Provider
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
  /** After this an unused link no longer connects an account (7 days after it was made). */
  expiresAt?: string | null
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

/** How a sign-in starts: on the sign-up screen, and with state to get back afterwards. */
export interface SignInOptions {
  signUp?: boolean
  state?: Record<string, unknown>
}

/** An external identity provider session (WorkOS AuthKit) the HTTP client can use. */
export interface AuthProvider {
  signedIn: boolean
  getToken(): Promise<string>
  signIn(options?: SignInOptions): Promise<void>
  signOut(): void
}

/** cur: the Cost and Usage Report; cost_explorer: provisional figures until it arrives. */
export type CostSource = 'cur' | 'cost_explorer'

/**
 * Month to date + dailyRunRate (the last runRateDays days of bills, before tax and monthly
 * charges) x the days left + monthlyCharges (once-a-month charges such as AWS Support not billed yet).
 */
export interface MonthEndForecast {
  amount: number
  method: 'run_rate'
  daysCovered: number
  daysInMonth: number
  runRateDays: number
  dailyRunRate: number
  monthlyCharges: number
}

/** USD forecasts; quote is today's rate for the workspace currency, which a scenario moves. */
export interface CostForecast {
  status: 'no_data' | 'ready'
  currency: string
  basis: 'official' | 'market'
  dataThrough: string | null
  quote: FxQuote | null
  monthEnd: { month: string; forecast: MonthEndForecast; low: number; high: number } | null
  /** The next three months: a bounded straight line through the last 90 days (run_rate under 28 days). */
  months: { month: string; amount: number; low: number; high: number; method: 'trend' | 'run_rate' }[]
  monthlyChargesUsd: number
  taxShare: number
  trendDays: number
  /** Last month against the month before, each day at its own rate: change = usage + FX effect. */
  decomposition: {
    month: string
    previousMonth: string
    usd: number
    previousUsd: number
    local: number | null
    previousLocal: number | null
    rate: number | null
    previousRate: number | null
    usageEffect: number | null
    fxEffect: number | null
  } | null
}

export interface CostContributor {
  serviceCode: string
  serviceName: string
  region: string | null
  usd: number
  previousUsd: number
  changeUsd: number
  /** null: nothing on the same days last month. */
  changePercent: number | null
  usageType: string | null
  usageTypeChangeUsd: number
  /** In USD; the page words it in the viewer's currency. */
  sentence: string
}

/** Month to date against the same days last month, by service and region (usage and purchases). */
export interface CostChanges {
  status: 'no_data' | 'ready'
  month: string | null
  current: { from: string; to: string } | null
  previous: { from: string; to: string } | null
  usd: number
  previousUsd: number
  changeUsd: number
  rises: CostContributor[]
  falls: CostContributor[]
}

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
  forecast: MonthEndForecast | null
  previousMonth: number | null
  /** The latest check of this or last month's totals against AWS Cost Explorer. */
  reconciliation?: {
    status: 'ok' | 'mismatch' | 'failed'
    month: string
    differencePct: number | null
    accountsMismatched: number
    checkedAt: string
  } | null
  /** current: this month so far; changing: an ended month AWS may still restate; final: the bill is final. */
  monthState?: MonthState
}

export type MonthState = 'current' | 'changing' | 'final'

/** A month with spend, newest first. report: its executive report is stored, or can be made as a draft. */
export interface CostMonth {
  month: string
  source: CostSource
  /** Amortised, after credits, with tax. */
  total: number
  dataThrough: string
  state: MonthState
  report: 'stored' | 'draft' | null
}

export interface CostMonths {
  currency: 'USD'
  months: CostMonth[]
}

/** One month of a reload: reloaded (stored again), unchanged (same figures), failed, or not_in_bucket. */
export interface PeriodReload {
  period: string
  outcome: 'reloaded' | 'unchanged' | 'failed' | 'not_in_bucket'
  version: string | null
  detail: string | null
}

/** A connection's latest reload of its bills. queued and running: still working; none: never reloaded. */
export interface ReloadStatus {
  connectionId: string
  status: 'none' | 'queued' | 'running' | 'skipped' | 'succeeded' | 'partial' | 'failed'
  /** YYYY-MM, or null for every month in the export. */
  period: string | null
  requestedAt: string | null
  finishedAt: string | null
  periods: PeriodReload[]
  error: string | null
  /** When another reload may start (one an hour per connection). */
  nextAllowedAt: string | null
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
  /** The cloud provider; absent from older payloads, which are all AWS. */
  provider?: Provider
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
  | 'not_verified'
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
  /** R7: the schedule tag to add and the scheduler stack that acts on it. */
  schedule?: ScheduleFix
  /** R10: route tables that still send traffic to the NAT gateway (the customer changes them). */
  routes?: NatRoutesFix
}

export interface NatRouteTable {
  routeTableId: string
  vpcId: string | null
  /** IPv4 or IPv6 CIDRs, or prefix list IDs, routed to the gateway. */
  destinations: string[]
  subnetIds: string[]
  /** The VPC's main route table, which also serves subnets without their own. */
  main: boolean
  consoleUrl: string
  inspect: string
  replace: string[]
  delete: string[]
}

export interface NatRoutesFix {
  natGatewayId: string
  tables: NatRouteTable[]
}

/** R7 non-production schedule (PRD FR-ANA-07): one tag, read by a stack the customer deploys. */
export interface ScheduleFix {
  tagKey: string
  /** For example ``07:00-20:00;MO-FR;Africa/Lagos`` (RDS: spaces instead of ``;``). */
  tagValue: string
  start: string
  end: string
  days: string
  timeZone: string
  hoursOnPerWeek: number
  stackName: string
  /** The CloudFormation template to download. */
  templateUrl: string
  /** One-click stack creation, once the template is hosted in S3. */
  quickCreateUrl: string | null
  /** RDS starts a stopped database again by itself after 7 days. */
  rdsAutoStart: boolean
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
  /** The cloud provider; absent from older payloads, which are all AWS. */
  provider?: Provider
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
  /** When a member marked it done. */
  doneAt?: string | null
  /** When a scan confirmed the change (PRD US-5.2). */
  verifiedAt?: string | null
  /** When the change happened, as near as the scans and the done date tell. */
  changedAt?: string | null
  verification?: RecommendationVerification | null
  /** Normalised to 30 days; null until 7 days of bills after the change are in. */
  realisedSavingsUsdMonth?: number | null
  /** cur: from the resource's own costs in the bills; estimate: the bills do not show it. */
  realisedBasis?: 'cur' | 'estimate' | null
  realisedAt?: string | null
}

export interface RecommendationVerification {
  /** What the last check saw, for example volume_deleted or instance_type_changed. */
  check: string
  outcome: 'implemented' | 'gone' | 'not_implemented'
  checkedAt: string
  /** The saving credited until the bills measure it. */
  estimateUsdMonth?: number
  measurement?: {
    beforeDays: number
    afterDays: number
    beforeUsdDay: number | null
    afterUsdDay: number | null
    measuredAt: string
  }
}

export interface RecommendationSummary {
  openCount: number
  savingsUsdMonth: number
  byRule: { ruleId: string; count: number; savingsUsdMonth: number }[]
  lastEvaluatedAt: string | null
  verifiedCount?: number
  /** Marked done, waiting for a scan to confirm it. */
  awaitingVerificationCount?: number
  /** Marked done, but the change was not seen 72 hours later. */
  notVerifiedCount?: number
  /** Realised savings a month measured from the bills, once per resource. */
  realisedSavingsUsdMonth?: number
  /** Verified, but not measured from the bills (yet): the estimate. */
  estimatedRealisedUsdMonth?: number
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
  /** A status change without an actor was made by Cloudoptics (a verification check). */
  kind: 'status_changed' | 'assigned' | 'commented' | 'savings_measured'
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
  /** The cloud provider; absent from older payloads, which are all AWS. */
  provider?: Provider
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
  /** Savings Plans and Reserved Instances ending in 60, 30 and 15 days. */
  commitmentExpiry: boolean
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
  /** The biggest change by service and region against the same days of the month before. */
  whatChanged?: string | null
  budgets: { name: string; currency: string; amount: number; status: string; actual: number | null; actualPercent: number | null }[]
  savings: {
    openCount: number
    openUsdMonth: number
    newCount: number
    doneCount: number
    doneUsdMonth: number
    /** Confirmed in AWS this month (absent from reports made before verification). */
    verifiedCount?: number
    realisedUsdMonth?: number
  }
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
  /** R7: a tag value (under these keys) or name matching this pattern is non-production. */
  nonprodPattern: string
  nonprodTagKeys: string[]
  /** R7: weekday office hours, HH:MM in the workspace's time zone. */
  scheduleStart: string
  scheduleEnd: string
}

export interface RuleSettingsView {
  rules: string[]
  settings: RuleSettings
}

export interface TenantChange {
  name?: string
  displayCurrency?: string
  timeZone?: string
}

export type DeletionStatus = 'scheduled' | 'cancelled' | 'completed' | 'failed'

/** Rows per table before and after a deletion, each checked to be zero afterwards. */
export interface DeletionVerification {
  tables: Record<string, { before: number; after: number }>
  rowsDeleted: number
  verified: boolean
  /** Connection deletions: the AWS accounts whose data went. */
  accounts?: string[]
  unusedTagSetsDeleted?: number
  /** Workspace deletions: what is kept (the audit log). */
  kept?: Record<string, number>
}

/** A request to delete a disconnected connection's data, or the whole workspace. */
export interface DataDeletion {
  id: string
  kind: 'connection' | 'tenant'
  connectionId: string | null
  /** The AWS account ID or the workspace name. */
  targetLabel: string
  status: DeletionStatus
  requestedAt: string
  requestedByEmail: string | null
  scheduledFor: string
  cancelledAt: string | null
  completedAt: string | null
  verification: DeletionVerification | null
  error: string | null
}

/** The CloudFormation stacks a connection created in the customer's AWS account. */
export interface StackSteps {
  awsAccountId: string
  /** The cloud provider; absent from older payloads, which are all AWS. */
  provider?: Provider
  accountType: string
  stacks: string[]
}

/** The workspace's scheduled deletion (null when none), and what to delete in AWS. */
export interface TenantDeletion {
  deletion: DataDeletion | null
  stacks: StackSteps[]
  stacksConsoleUrl: string
  graceDays: number
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

export type CommitmentKind = 'savings_plan' | 'ec2_reserved' | 'rds_reserved'

/** A Savings Plan or Reserved Instance, as the daily refresh last listed it. */
export interface Commitment {
  id: string
  connectionId: string
  kind: CommitmentKind
  commitmentId: string
  accountId: string | null
  /** The cloud provider; absent from older payloads, which are all AWS. */
  provider?: Provider
  regionId: string | null
  description: string
  planType: string | null
  termMonths: number | null
  paymentOption: string | null
  state: string | null
  quantity: number | null
  hourlyCommitmentUsd: number | null
  upfrontUsd: number | null
  startsAt: string | null
  endsAt: string | null
  /** Last 30 days, from Cost Explorer. */
  utilisationPct: number | null
  daysLeft: number | null
  /** False once it ended or the refresh stopped listing it. */
  current: boolean
  lastSeenAt: string
}

/** Cost Explorer's 30-day view per connection (payer). */
export interface PayerCoverage {
  connectionId: string
  savingsPlansCoveragePercent: number | null
  savingsPlansUtilisationPercent: number | null
  reservationsCoveragePercent: number | null
  reservationsUtilisationPercent: number | null
  fetchedAt: string
  errors: string[]
}

export interface CommitmentList {
  commitments: Commitment[]
  coverage: PayerCoverage[]
  expiryAlertDays: number[]
}

export type CommitmentPayment = 'no_upfront' | 'partial_upfront' | 'all_upfront'

/** One cell of the Savings Plan advisory matrix. Break-even in months; 0 = saves from the first hour. */
export interface CommitmentOption {
  source: 'ours' | 'aws'
  instrument: 'savings_plan' | 'reserved_instance'
  termYears: number
  payment: CommitmentPayment
  lookbackDays: number | null
  discountPercent: number
  hourlyCommitmentUsd: number
  coveredOnDemandHourlyUsd: number
  monthlySavingsUsd: number
  upfrontUsd: number
  monthlyRecurringUsd: number
  totalCommitmentUsd: number
  breakEvenMonths: number
  /** The discount is AWS's estimate for this account, not the typical one. */
  discountFromAws: boolean
}

export interface SavingsPlanAdvice {
  connectionId: string
  payerAccountId: string
  status: 'no_data' | 'not_enough_data' | 'no_purchase' | 'ok'
  dataThrough: string | null
  baseline: {
    days: number
    nettedUsdMonth: number
    averageHourlyUsd: number
    p10HourlyUsd: number
    p20HourlyUsd: number
    targetHourlyUsd: number
    bandLowHourlyUsd: number
    bandHighHourlyUsd: number
  }
  dailyEligible: { date: string; amount: number }[]
  options: CommitmentOption[]
  default: CommitmentOption | null
  ladder: { month: number; buyOn: string; buyHourlyUsd: number; totalHourlyUsd: number; sharePercent: number }[]
  fx: {
    currency: string
    rate: number
    rateDate: string
    estimated: boolean
    scenarios: { weakeningPercent: number; rate: number; monthlyLocal: number; upfrontLocal: number; totalLocal: number }[]
  } | null
  fxExposed: boolean
  currentCoveragePercent: number | null
  targetCoveragePercent: number | null
  utilisationPercent: number | null
  declineTolerancePercent: number | null
  awsFetchedAt: string | null
  awsErrors: string[]
}

export interface SavingsPlanAdvisory {
  advice: SavingsPlanAdvice[]
  returnWindowDays: number
  ladderFirstPercent: number
  ladderStepPercent: number
  fxStressPercent: number[]
}

export type ExportKind = 'costs' | 'recommendations' | 'anomalies' | 'budgets'
export type CostExportGroup = 'service' | 'account' | 'region' | 'usage_type' | 'tag'

/** What to export (PRD US-7.2): a cost table or a list, with the page's filters. */
export type ExportRequest =
  | {
      kind: 'costs'
      /** Inclusive dates (YYYY-MM-DD), at most 400 days apart. */
      start: string
      end: string
      granularity?: 'day' | 'month'
      groupBy?: CostExportGroup[]
      /** Needed when grouping by tag. */
      tagKey?: string | null
      accountId?: string | null
      includeTax?: boolean
    }
  | {
      kind: 'recommendations'
      status?: 'open' | 'all'
      ruleId?: string | null
      accountId?: string | null
      risk?: 'low' | 'medium' | 'high' | null
      alternatives?: boolean
    }
  | { kind: 'anomalies'; days?: number; status?: AnomalyStatus | null }
  | { kind: 'budgets' }

export type ExportStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'expired'

/** An export and its file (kept 7 days). Small exports are `succeeded` at once; large ones run on the worker. */
export interface ExportJob {
  id: string
  kind: ExportKind
  params: Record<string, unknown>
  status: ExportStatus
  progress: number
  rowCount: number | null
  fileName: string
  sizeBytes: number | null
  error: string | null
  createdAt: string
  startedAt: string | null
  finishedAt: string | null
  expiresAt: string
  downloadUrl: string | null
}

export interface Api {
  readonly mode: 'http' | 'demo'
  authConfig(): Promise<AuthConfig>
  isSignedIn(): boolean
  signIn(email: string, name?: string, options?: SignInOptions): Promise<void>
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
  /** Any role. 429 after 10 exports in an hour. */
  createExport(tenantId: string, request: ExportRequest): Promise<ExportJob>
  getExport(tenantId: string, id: string): Promise<ExportJob>
  /** Your exports from the last 7 days, newest first. */
  listExports(tenantId: string): Promise<{ exports: ExportJob[] }>
  /** The CSV of a finished export (410 once expired). */
  downloadExport(tenantId: string, id: string): Promise<Blob>
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
  /** Admins only, once disconnected: `confirm` is the connection's AWS account ID. */
  deleteConnectionData(tenantId: string, connectionId: string, confirm: string): Promise<DataDeletion>
  /** Admins only: every deletion request, newest first, with its verification. */
  listDeletions(tenantId: string): Promise<DataDeletion[]>
  /** Admins only: the workspace's scheduled deletion (if any) and the AWS stacks to delete. */
  getTenantDeletion(tenantId: string): Promise<TenantDeletion>
  /** Admins only: `confirmName` is the workspace name. Deleted after the grace period. */
  scheduleTenantDeletion(tenantId: string, confirmName: string): Promise<TenantDeletion>
  cancelTenantDeletion(tenantId: string): Promise<void>
  /** Look for the resource role now. `queued` means the worker scans within a minute or two. */
  scanResources(tenantId: string, connectionId: string): Promise<ResourceScan>
  /** What the scans found, and each connection's latest scan. */
  resourceSummary(tenantId: string): Promise<InventorySummary>
  /** month (YYYY-MM): an ended month instead of the current one. */
  costSummary(tenantId: string, month?: string | null): Promise<CostSummary>
  /** month: every day of that ended month instead of the last `days` days. */
  costDaily(tenantId: string, days?: number, month?: string | null): Promise<DailyCosts>
  costBreakdown(tenantId: string, dimension: CostDimension, limit?: number, month?: string | null): Promise<CostBreakdown>
  /** The months with cost data, newest first. */
  costMonths(tenantId: string): Promise<CostMonths>
  fxCurrent(tenantId: string): Promise<CurrentRate>
  costForecast(tenantId: string): Promise<CostForecast>
  /** month: that whole ended month against the whole month before. */
  costChanges(tenantId: string, month?: string | null): Promise<CostChanges>
  /** Admins only: read the bills again from the export bucket (period YYYY-MM, or null for every month). Once an hour. */
  reloadBills(tenantId: string, connectionId: string, period: string | null): Promise<ReloadStatus>
  /** Admins only: the connection's latest reload. */
  reloadStatus(tenantId: string, connectionId: string): Promise<ReloadStatus>
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
  listCommitments(tenantId: string): Promise<CommitmentList>
  savingsPlanAdvisory(tenantId: string): Promise<SavingsPlanAdvisory>
  /** Admins only: read commitments and ask Cost Explorer again now (about USD 0.08 a connection). */
  refreshCommitments(tenantId: string): Promise<{ status: string; connections: number }>
  /** Staff only (platform admins with MFA): every grant, newest first. */
  listSupportGrants(): Promise<SupportGrant[]>
  /** Staff only: open read-only access to one workspace now; its admins are emailed. */
  requestSupportGrant(body: SupportGrantRequest): Promise<SupportGrant>
  /** Staff only: end an active grant now. */
  endSupportGrant(grantId: string): Promise<SupportGrant>
}

export class ApiError extends Error {
  readonly status: number
  readonly fieldErrors: { field: string; message: string }[]
  /** Machine-readable problem code (docs/09 §3.4), e.g. 'mfa_required'. */
  readonly code: string | undefined
  /** Seconds to wait before trying again (429 ``Retry-After``). */
  readonly retryAfter: number | undefined

  constructor(
    status: number,
    title: string,
    fieldErrors: { field: string; message: string }[] = [],
    code?: string,
    retryAfter?: number,
  ) {
    super(title)
    this.status = status
    this.fieldErrors = fieldErrors
    this.code = code
    this.retryAfter = retryAfter
  }
}

/** Shown when the API refuses a request because of its rate limits (429). */
export function rateLimitedMessage(seconds: number | undefined): string {
  const wait = seconds && seconds > 0 ? `in ${seconds} second${seconds === 1 ? '' : 's'}` : 'in a minute'
  return `Too many requests right now. Wait a moment and try again ${wait}.`
}

export function isRateLimited(error: unknown): error is ApiError {
  return error instanceof ApiError && error.status === 429
}

/** Shown when a workspace requires MFA and this session signed in without it. */
export const MFA_REQUIRED_MESSAGE =
  'This workspace requires multi-factor authentication. Turn on MFA (an authenticator app) in your account, then sign out and sign in again.'

export function isMfaRequired(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'mfa_required'
}
