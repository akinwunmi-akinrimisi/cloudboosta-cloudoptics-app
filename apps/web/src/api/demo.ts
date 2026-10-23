import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type {
  AccountList,
  Api,
  AuditEntry,
  AuditPage,
  CloudAccount,
  AuthConfig,
  Connection,
  ConnectionCreate,
  ConnectionSetup,
  CostBreakdown,
  CostDimension,
  CostSummary,
  CurrentRate,
  DailyCosts,
  Me,
  PermissionCheck,
  Recommendation,
  RecommendationQuery,
  RecommendationStatus,
  RecommendationSummary,
  StatusChange,
  Tenant,
} from './types'
import { ApiError } from './types'

// In-browser stand-in for the API, used by the public GitHub Pages build. Nothing leaves
// the browser and no AWS call is made; state lives in this viewer's localStorage only.

const KEY = 'co.demo.v1'
// Illustrative rates for the demo only; real rates are set by Cloudoptics staff (FR-FX-06).
const DEMO_RATES: Record<string, number> = {
  NGN: 1530.25,
  KES: 129.4,
  GHS: 15.5,
  ZAR: 18.1,
  EGP: 48.6,
  RWF: 1410,
  UGX: 3690,
  TZS: 2600,
  GBP: 0.79,
  EUR: 0.92,
}
const COUNTRY: Record<string, [string, string]> = {
  NG: ['NGN', 'Africa/Lagos'],
  KE: ['KES', 'Africa/Nairobi'],
  GH: ['GHS', 'Africa/Accra'],
  ZA: ['ZAR', 'Africa/Johannesburg'],
  EG: ['EGP', 'Africa/Cairo'],
  RW: ['RWF', 'Africa/Kigali'],
  UG: ['UGX', 'Africa/Kampala'],
  TZ: ['TZS', 'Africa/Dar_es_Salaam'],
  GB: ['GBP', 'Europe/London'],
}

interface State {
  me: Me | null
  tenants: Tenant[]
  connections: Record<string, Connection[]>
  externalIds: Record<string, string>
  /** tenantId:recommendationId -> status changed in the demo. */
  recommendationStatus: Record<string, RecommendationStatus>
}

const empty = (): State => ({
  me: null,
  tenants: [],
  connections: {},
  externalIds: {},
  recommendationStatus: {},
})

const OPEN: RecommendationStatus[] = ['new', 'assigned', 'in_progress']

type DemoRec = Pick<
  Recommendation,
  'id' | 'ruleId' | 'type' | 'accountId' | 'regionId' | 'resourceId' | 'title' | 'costNowUsdMonth' | 'costAfterUsdMonth'
> &
  Partial<Recommendation>

// Illustrative findings for the demo estate (same accounts and regions as the cost data).
const DEMO_RECS: DemoRec[] = [
  {
    id: 'demo-rec-1',
    ruleId: 'R1_unattached_ebs',
    type: 'delete_unattached_volume',
    accountId: '333333333333',
    regionId: 'eu-west-1',
    resourceId: 'vol-0a1b2c3d4e5f60718',
    title: 'Delete unattached gp2 volume build-cache (500 GiB)',
    costNowUsdMonth: 55,
    costAfterUsdMonth: 0,
    reversibility: 'with_snapshot',
    riskScore: 30,
    riskFactors: [{ factor: 'no_recent_snapshot', points: 30 }],
    evidence: [{ kind: 'state', value: 'available', sinceDays: 41 }],
    fix: {
      steps: [
        "aws ec2 create-snapshot --volume-id vol-0a1b2c3d4e5f60718 --description 'CloudOptics backup of vol-0a1b2c3d4e5f60718' --region eu-west-1",
        'aws ec2 wait snapshot-completed --filters Name=volume-id,Values=vol-0a1b2c3d4e5f60718 --region eu-west-1',
        'aws ec2 delete-volume --volume-id vol-0a1b2c3d4e5f60718 --region eu-west-1',
      ],
      rollback: [
        'aws ec2 create-volume --snapshot-id <snapshot-id> --availability-zone eu-west-1a --volume-type gp2 --region eu-west-1',
      ],
      notes: 'Keep the snapshot until you are sure the data is not needed.',
    },
  },
  {
    id: 'demo-rec-2',
    ruleId: 'R3_gp2_gp3',
    type: 'modify_volume_gp3',
    accountId: '111111111111',
    regionId: 'eu-west-1',
    resourceId: 'vol-0f9e8d7c6b5a40312',
    title: 'Change volume db-data from gp2 to gp3 (1000 GiB)',
    costNowUsdMonth: 110,
    costAfterUsdMonth: 98.8,
    reversibility: 'reversible',
    riskScore: 25,
    riskFactors: [{ factor: 'production', points: 25 }],
    recommendedConfig: { volumeType: 'gp3', iops: 3000, throughput: 250 },
    evidence: [{ kind: 'volumeType', value: 'gp2', sizeGiB: 1000 }],
    fix: {
      steps: [
        'aws ec2 modify-volume --volume-id vol-0f9e8d7c6b5a40312 --volume-type gp3 --iops 3000 --throughput 250 --region eu-west-1',
      ],
      rollback: ['aws ec2 modify-volume --volume-id vol-0f9e8d7c6b5a40312 --volume-type gp2 --region eu-west-1'],
      notes: 'Online change, no restart. A volume can be modified once every 6 hours.',
    },
  },
  {
    id: 'demo-rec-3',
    ruleId: 'R2_idle_lb',
    type: 'delete_idle_load_balancer',
    accountId: '333333333333',
    regionId: 'af-south-1',
    resourceId: 'staging-old-alb',
    title: 'Delete idle application load balancer staging-old-alb (no healthy targets)',
    costNowUsdMonth: 21.39,
    costAfterUsdMonth: 0,
    riskScore: 0,
    riskFactors: [],
    confidenceNotes: ['hourly_charge_only_lcu_excluded'],
    evidence: [{ kind: 'idleDays', value: 19 }],
    fix: {
      steps: [
        'aws elbv2 delete-load-balancer --load-balancer-arn arn:aws:elasticloadbalancing:af-south-1:333333333333:loadbalancer/app/staging-old-alb/50dc6c495c0c9188 --region af-south-1',
      ],
      rollback: [],
      notes: 'Check DNS records and listeners pointing at it before deleting.',
    },
  },
  {
    id: 'demo-rec-4',
    ruleId: 'R1_old_snapshot',
    type: 'delete_old_snapshot',
    accountId: '111111111111',
    regionId: 'eu-west-1',
    resourceId: 'snap-07c1d2e3f4a5b6c7d',
    title: 'Delete 412-day-old snapshot snap-07c1d2e3f4a5b6c7d of a deleted volume',
    costNowUsdMonth: 10,
    costAfterUsdMonth: 0,
    riskScore: 55,
    riskFactors: [
      { factor: 'production', points: 25 },
      { factor: 'data_loss', points: 30 },
    ],
    confidence: 'medium',
    confidenceNotes: ['savings_upper_bound_incremental'],
    evidence: [{ kind: 'sourceVolume', value: 'vol-0123456789abcdef0', exists: false }],
    fix: {
      steps: ['aws ec2 delete-snapshot --snapshot-id snap-07c1d2e3f4a5b6c7d --region eu-west-1'],
      rollback: [],
      notes: 'Deleting a snapshot cannot be undone.',
      alternative: {
        title: 'Archive instead (about 75% cheaper, 90-day minimum)',
        monthlyCostUsd: 2.5,
        steps: [
          'aws ec2 modify-snapshot-tier --snapshot-id snap-07c1d2e3f4a5b6c7d --storage-tier archive --region eu-west-1',
        ],
      },
    },
  },
  {
    id: 'demo-rec-5',
    ruleId: 'R2_idle_eip',
    type: 'release_idle_ip',
    accountId: '333333333333',
    regionId: 'af-south-1',
    resourceId: 'eipalloc-0a1b2c3d4e5f6a7b8',
    title: 'Release idle Elastic IP 203.0.113.25 (not attached to anything)',
    costNowUsdMonth: 3.65,
    costAfterUsdMonth: 0,
    riskScore: 0,
    riskFactors: [],
    evidence: [{ kind: 'state', value: 'unassociated', sinceDays: 12 }],
    fix: {
      steps: ['aws ec2 release-address --allocation-id eipalloc-0a1b2c3d4e5f6a7b8 --region af-south-1'],
      rollback: [],
      notes: 'A released address cannot be got back; update DNS or allow-lists that use it first.',
    },
  },
]

function demoRecommendation(r: DemoRec, status: RecommendationStatus): Recommendation {
  const savings = Math.round((r.costNowUsdMonth - r.costAfterUsdMonth) * 100) / 100
  const riskScore = r.riskScore ?? 0
  const riskLevel = riskScore > 65 ? 'high' : riskScore >= 35 ? 'medium' : 'low'
  const confidence = r.confidence ?? 'high'
  return {
    currentConfig: {},
    recommendedConfig: { action: 'delete' },
    savingsUsdMonth: savings,
    savingsBasis: 'effective_rate',
    effort: 'S',
    reversibility: 'irreversible',
    restartRequired: false,
    riskFactors: [],
    confidenceNotes: [],
    evidence: [],
    primaryOf: null,
    firstDetectedAt: new Date(Date.now() - 6 * 86_400_000).toISOString(),
    lastEvaluatedAt: new Date(Date.now() - 3 * 3_600_000).toISOString(),
    ...r,
    riskScore,
    riskLevel,
    confidence,
    priorityScore:
      savings * { high: 1, medium: 0.7, low: 0.4 }[confidence] * { low: 1, medium: 0.6, high: 0.3 }[riskLevel],
    fix: r.fix ?? { steps: [], rollback: [] },
    status,
  }
}

function load(): State {
  const raw = read('local', KEY)
  if (!raw) return empty()
  try {
    return { ...empty(), ...(JSON.parse(raw) as State) }
  } catch {
    return empty()
  }
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms))

function randomId(): string {
  return crypto.randomUUID()
}

function externalId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

export class DemoApi implements Api {
  readonly mode = 'demo' as const
  private state: State = load()
  private readonly latencyMs: number

  constructor(latencyMs = 250) {
    this.latencyMs = latencyMs
  }

  private save(): void {
    write('local', KEY, JSON.stringify(this.state))
  }

  private async tick(): Promise<void> {
    if (this.latencyMs) await delay(this.latencyMs)
  }

  private requireMe(): Me {
    if (!this.state.me) throw new ApiError(401, 'Authentication required')
    return this.state.me
  }

  private tenant(tenantId: string): Tenant {
    const t = this.state.tenants.find((x) => x.id === tenantId)
    if (!t) throw new ApiError(404, 'Workspace not found')
    return t
  }

  async authConfig(): Promise<AuthConfig> {
    return {
      mode: 'dev',
      configured: true,
      provider: null,
      issuer: null,
      clientId: null,
    }
  }

  isSignedIn(): boolean {
    return this.state.me !== null
  }

  async signIn(email: string, name?: string): Promise<void> {
    await this.tick()
    const existing = this.state.me
    this.state.me =
      existing && existing.email === email.toLowerCase()
        ? existing
        : {
            id: randomId(),
            email: email.toLowerCase(),
            displayName: name ?? null,
            preferredCurrencyView: 'both',
            memberships: [],
          }
    if (existing && existing.email !== email.toLowerCase()) {
      this.state = { ...empty(), me: this.state.me }
    }
    this.save()
  }

  signOut(): void {
    this.state = empty()
    this.save()
  }

  async me(): Promise<Me> {
    await this.tick()
    return structuredClone(this.requireMe())
  }

  async updateMe(view: CurrencyMode): Promise<Me> {
    const me = this.requireMe()
    me.preferredCurrencyView = view
    this.save()
    return structuredClone(me)
  }

  async createTenant(body: { name: string; countryCode: string }): Promise<Tenant> {
    await this.tick()
    const me = this.requireMe()
    const name = body.name.trim()
    if (!name) throw new ApiError(422, 'Request validation failed', [{ field: 'name', message: 'Enter a name' }])
    const [currency, tz] = COUNTRY[body.countryCode] ?? ['USD', 'UTC']
    const tenant: Tenant = {
      id: randomId(),
      name,
      countryCode: body.countryCode,
      displayCurrency: currency,
      timeZone: tz,
      mfaRequired: false,
      status: 'active',
      createdAt: new Date().toISOString(),
      role: 'admin',
    }
    this.state.tenants.push(tenant)
    me.memberships.push({
      tenantId: tenant.id,
      tenantName: name,
      role: 'admin',
    })
    this.save()
    return structuredClone(tenant)
  }

  async getTenant(tenantId: string): Promise<Tenant> {
    await this.tick()
    return structuredClone(this.tenant(tenantId))
  }

  async listConnections(tenantId: string): Promise<Connection[]> {
    await this.tick()
    this.tenant(tenantId)
    return structuredClone(this.state.connections[tenantId] ?? [])
  }

  async createConnection(tenantId: string, body: ConnectionCreate): Promise<ConnectionSetup> {
    await this.tick()
    this.tenant(tenantId)
    if (!/^\d{12}$/.test(body.awsAccountId)) {
      throw new ApiError(422, 'Request validation failed', [
        {
          field: 'awsAccountId',
          message: 'An AWS account ID has exactly 12 digits',
        },
      ])
    }
    const all = Object.values(this.state.connections).flat()
    const own = (this.state.connections[tenantId] ?? []).find((c) => c.awsAccountId === body.awsAccountId)
    if (own && own.status !== 'disconnected') {
      throw new ApiError(
        409,
        'This AWS account is already connected to this workspace. Use Set up again or Disconnect on the Connections page.',
      )
    }
    if (own) {
      // Reconnect: same connection, fresh External ID, back to pending.
      Object.assign(own, {
        accountType: body.accountType,
        createCurExport: body.createCurExport,
        status: 'pending',
        curBucket: null,
        curPrefix: null,
        curExportArn: null,
        permissionChecks: [],
        lastError: null,
        lastValidatedAt: null,
      })
      this.state.externalIds[own.id] = externalId()
      this.save()
      return this.setup(own)
    }
    if (all.some((c) => c.awsAccountId === body.awsAccountId && c.status !== 'disconnected')) {
      throw new ApiError(409, 'This AWS account is already connected to a Cloudoptics workspace.')
    }
    const conn: Connection = {
      id: randomId(),
      awsAccountId: body.awsAccountId,
      accountType: body.accountType,
      roleArn: `arn:aws:iam::${body.awsAccountId}:role/CloudopticsBillingReadOnly`,
      status: 'pending',
      createCurExport: body.createCurExport,
      curBucket: null,
      curPrefix: null,
      curExportArn: null,
      permissionChecks: [],
      lastError: null,
      lastValidatedAt: null,
      createdAt: new Date().toISOString(),
    }
    ;(this.state.connections[tenantId] ??= []).push(conn)
    this.state.externalIds[conn.id] = externalId()
    this.save()
    return this.setup(conn)
  }

  async disconnectConnection(tenantId: string, connectionId: string): Promise<void> {
    await this.tick()
    this.connection(tenantId, connectionId).status = 'disconnected'
    this.save()
  }

  async getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup> {
    await this.tick()
    return this.setup(this.connection(tenantId, connectionId))
  }

  async validateConnection(tenantId: string, connectionId: string): Promise<Connection> {
    await delay(this.latencyMs * 4)
    const conn = this.connection(tenantId, connectionId)
    const bucket = `cloudoptics-cur-${conn.awsAccountId}`
    const checks: PermissionCheck[] = [
      {
        id: 'assume_role',
        label: 'Assume the Cloudoptics role',
        status: 'ok',
        message: 'Role assumed.',
        hint: null,
      },
      {
        id: 'cost_explorer',
        label: 'Read Cost Explorer',
        status: 'ok',
        message: 'Cost data readable.',
        hint: null,
      },
      {
        id: 'cur_export',
        label: 'Find the CUR 2.0 export',
        status: 'ok',
        message: 'Found export cloudoptics-cur2.',
        hint: null,
      },
      {
        id: 'cur_delivery',
        label: 'Read the export bucket',
        status: 'warning',
        message: 'The export exists; its first delivery usually arrives within 24 hours.',
        hint: 'We will check again automatically.',
      },
      {
        id: 'organization',
        label: 'Check the account type',
        status: 'ok',
        message: 'Account type confirmed.',
        hint: null,
      },
      {
        id: 'savings_plans',
        label: 'Read Savings Plans',
        status: 'ok',
        message: 'Savings Plans readable.',
        hint: null,
      },
    ]
    Object.assign(conn, {
      status: 'healthy',
      permissionChecks: checks,
      curBucket: bucket,
      curPrefix: 'cloudoptics',
      curExportArn: `arn:aws:bcm-data-exports:us-east-1:${conn.awsAccountId}:export/cloudoptics-cur2-demo`,
      lastError: null,
      lastValidatedAt: new Date().toISOString(),
    })
    this.save()
    return structuredClone(conn)
  }

  async costSummary(tenantId: string): Promise<CostSummary> {
    await this.tick()
    this.tenant(tenantId)
    const now = new Date()
    const month = now.toISOString().slice(0, 7)
    const connected = (this.state.connections[tenantId] ?? []).filter((c) => c.status !== 'disconnected')
    const empty: CostSummary = {
      status: connected.length ? 'waiting' : 'no_connection',
      source: null,
      month,
      currency: 'USD',
      dataThrough: null,
      lastLoadAt: null,
      lastLoadStatus: null,
      monthToDate: null,
      forecast: null,
      previousMonth: null,
    }
    if (!this.hasData(tenantId)) return empty
    // Illustrative figures in the shape of tools/cur-fixture: about USD 41 a day.
    const days = Math.max(now.getUTCDate() - 1, 1)
    const daysInMonth = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).getUTCDate()
    const daily = 41.26
    const through = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), days))
    return {
      ...empty,
      status: 'ready',
      source: 'cur',
      dataThrough: through.toISOString().slice(0, 10),
      lastLoadAt: now.toISOString(),
      lastLoadStatus: 'succeeded',
      monthToDate: {
        effective: daily * days,
        billed: daily * days,
        credits: -2 * days,
      },
      forecast: {
        amount: daily * daysInMonth,
        method: 'run_rate',
        daysCovered: days,
        daysInMonth,
      },
      previousMonth: 1237.75,
    }
  }

  async costDaily(tenantId: string, days = 30): Promise<DailyCosts> {
    await this.tick()
    this.tenant(tenantId)
    if (!this.hasData(tenantId)) return { source: null, currency: 'USD', points: [] }
    const today = Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate())
    const points = Array.from({ length: days }, (_, i) => {
      const day = new Date(today - (days - i) * 86_400_000)
      // Weekdays cost more than weekends, like a typical dev/test estate.
      const weekend = day.getUTCDay() === 0 || day.getUTCDay() === 6
      const amount = (weekend ? 31.4 : 45.2) + ((i * 7) % 5) * 0.9
      return {
        date: day.toISOString().slice(0, 10),
        amount: Math.round(amount * 100) / 100,
      }
    })
    return { source: 'cur', currency: 'USD', points }
  }

  // The demo estate has fewer items than any limit, so ``limit`` changes nothing here.
  async costBreakdown(tenantId: string, dimension: CostDimension, _limit = 8): Promise<CostBreakdown> {
    await this.tick()
    this.tenant(tenantId)
    const month = new Date().toISOString().slice(0, 7)
    if (!this.hasData(tenantId))
      return {
        source: null,
        month,
        dimension,
        currency: 'USD',
        total: 0,
        items: [],
      }
    const rows: Record<CostDimension, [string, string, number][]> = {
      service: [
        ['AmazonEC2', 'Amazon Elastic Compute Cloud', 512.4],
        ['AmazonRDS', 'Amazon Relational Database Service', 214.9],
        ['AmazonS3', 'Amazon Simple Storage Service', 88.3],
        ['AWSELB', 'Elastic Load Balancing', 61.7],
        ['AmazonCloudWatch', 'AmazonCloudWatch', 24.1],
      ],
      account: [
        ['111111111111', 'demo-prod', 598.2],
        ['333333333333', 'demo-dev', 303.2],
      ],
      region: [
        ['eu-west-1', 'eu-west-1', 640.8],
        ['af-south-1', 'af-south-1', 236.5],
        ['global', 'Global', 24.1],
      ],
    }
    const total = rows[dimension].reduce((sum, [, , amount]) => sum + amount, 0)
    return {
      source: 'cur',
      month,
      dimension,
      currency: 'USD',
      total: Math.round(total * 100) / 100,
      items: rows[dimension].map(([key, label, amount]) => ({
        key,
        label,
        amount,
        share: Math.round((amount / total) * 10_000) / 10_000,
      })),
    }
  }

  async fxCurrent(tenantId: string): Promise<CurrentRate> {
    await this.tick()
    const tenant = this.tenant(tenantId)
    const currency = tenant.displayCurrency
    const rate = currency === 'USD' ? 1 : DEMO_RATES[currency]
    return {
      currency,
      basis: 'official',
      quote: rate
        ? {
            currency,
            rate,
            rateDate: new Date().toISOString().slice(0, 10),
            basis: 'official',
            source: 'manual',
            estimated: false,
          }
        : null,
    }
  }

  async listAccounts(tenantId: string): Promise<AccountList> {
    await this.tick()
    this.tenant(tenantId)
    const now = new Date().toISOString()
    const accounts: CloudAccount[] = []
    for (const c of this.state.connections[tenantId] ?? []) {
      if (c.status === 'disconnected' || c.status === 'pending') continue
      const usable = c.status === 'healthy' || c.status === 'degraded'
      const own: CloudAccount = {
        awsAccountId: c.awsAccountId,
        name: c.accountType === 'management' ? 'payer' : null,
        accountType: c.accountType,
        orgStatus: c.accountType === 'management' && usable ? 'ACTIVE' : null,
        orgSeenAt: c.accountType === 'management' && usable ? now : null,
        resourceAccess: false,
        hasCostData: usable,
      }
      accounts.push(own)
      if (c.accountType === 'management' && usable) {
        accounts.push(
          {
            ...own,
            awsAccountId: '111111111111',
            name: 'demo-prod',
            accountType: 'member',
          },
          {
            ...own,
            awsAccountId: '333333333333',
            name: 'demo-dev',
            accountType: 'member',
          },
          {
            ...own,
            awsAccountId: '444444444444',
            name: 'sandbox',
            accountType: 'member',
            hasCostData: false,
          },
        )
      }
    }
    const current = accounts.filter((a) => a.orgStatus !== 'NOT_LISTED')
    return {
      coverage: {
        accounts: current.length,
        withCostData: current.filter((a) => a.hasCostData).length,
        withResourceAccess: current.filter((a) => a.resourceAccess).length,
      },
      accounts,
    }
  }

  private recommendations(tenantId: string): Recommendation[] {
    this.tenant(tenantId)
    if (!this.hasData(tenantId)) return []
    return DEMO_RECS.map((r) => demoRecommendation(r, this.state.recommendationStatus[`${tenantId}:${r.id}`] ?? 'new'))
  }

  async listRecommendations(tenantId: string, query: RecommendationQuery = {}): Promise<Recommendation[]> {
    await this.tick()
    return this.recommendations(tenantId)
      .filter((r) => query.status === 'all' || OPEN.includes(r.status))
      .sort((a, b) => b.priorityScore - a.priorityScore)
  }

  async recommendationSummary(tenantId: string): Promise<RecommendationSummary> {
    await this.tick()
    const open = this.recommendations(tenantId).filter((r) => OPEN.includes(r.status))
    const byRule = new Map<string, { ruleId: string; count: number; savingsUsdMonth: number }>()
    for (const r of open) {
      const row = byRule.get(r.ruleId) ?? {
        ruleId: r.ruleId,
        count: 0,
        savingsUsdMonth: 0,
      }
      row.count += 1
      row.savingsUsdMonth += r.savingsUsdMonth
      byRule.set(r.ruleId, row)
    }
    return {
      openCount: open.length,
      savingsUsdMonth: Math.round(open.reduce((sum, r) => sum + r.savingsUsdMonth, 0) * 100) / 100,
      byRule: [...byRule.values()].sort((a, b) => b.savingsUsdMonth - a.savingsUsdMonth),
      lastEvaluatedAt: open[0]?.lastEvaluatedAt ?? null,
    }
  }

  async getRecommendation(tenantId: string, id: string): Promise<Recommendation> {
    await this.tick()
    const rec = this.recommendations(tenantId).find((r) => r.id === id)
    if (!rec) throw new ApiError(404, 'Recommendation not found')
    return rec
  }

  async updateRecommendation(tenantId: string, id: string, change: StatusChange): Promise<Recommendation> {
    if (change.status === 'dismissed' && !change.reason?.trim()) {
      throw new ApiError(422, 'A reason is required to dismiss a recommendation.')
    }
    await this.getRecommendation(tenantId, id)
    this.state.recommendationStatus[`${tenantId}:${id}`] = change.status
    this.save()
    return this.getRecommendation(tenantId, id)
  }

  // The demo has no stored audit trail: it lists what this browser's state implies.
  async auditLog(tenantId: string): Promise<AuditPage> {
    await this.tick()
    const tenant = this.tenant(tenantId)
    const me = this.requireMe()
    const actor = { type: 'user' as const, email: me.email, name: me.displayName }
    const entries: Omit<AuditEntry, 'id'>[] = [
      {
        occurredAt: tenant.createdAt,
        actor,
        action: 'tenant.created',
        targetType: 'tenant',
        targetId: tenant.id,
        before: null,
        after: { name: tenant.name, countryCode: tenant.countryCode },
        ip: null,
      },
    ]
    for (const c of this.state.connections[tenantId] ?? []) {
      entries.push({
        occurredAt: c.createdAt,
        actor,
        action: 'connection.created',
        targetType: 'cloud_connection',
        targetId: c.id,
        before: null,
        after: { awsAccountId: c.awsAccountId, accountType: c.accountType },
        ip: null,
      })
      if (c.lastValidatedAt && c.status !== 'pending') {
        entries.push({
          occurredAt: c.lastValidatedAt,
          actor,
          action: 'connection.status_changed',
          targetType: 'cloud_connection',
          targetId: c.id,
          before: { status: 'pending' },
          after: { status: c.status },
          ip: null,
        })
      }
    }
    entries.sort((a, b) => b.occurredAt.localeCompare(a.occurredAt))
    return { entries: entries.map((e, i) => ({ ...e, id: entries.length - i })), nextBefore: null, verified: true }
  }

  private hasData(tenantId: string): boolean {
    return (this.state.connections[tenantId] ?? []).some((c) => c.status === 'healthy' || c.status === 'degraded')
  }

  private connection(tenantId: string, connectionId: string): Connection {
    this.tenant(tenantId)
    const conn = (this.state.connections[tenantId] ?? []).find((c) => c.id === connectionId)
    if (!conn) throw new ApiError(404, 'Connection not found')
    return conn
  }

  private setup(conn: Connection): ConnectionSetup {
    const base = `${import.meta.env.BASE_URL}cfn`
    return {
      connection: structuredClone(conn),
      externalId: this.state.externalIds[conn.id] ?? '',
      // The demo has no S3-hosted template, so it shows the download path, like staging today.
      quickCreateUrl: null,
      templateUrl: `${base}/billing-role.yaml`,
      templateDownloadUrl: `${base}/billing-role.yaml`,
      resourceTemplateUrl: `${base}/resource-role.yaml`,
      resourceTemplateDownloadUrl: `${base}/resource-role.yaml`,
      stackRegion: 'us-east-1',
      principalArn: 'arn:aws:iam::000000000000:role/CloudopticsBroker',
    }
  }
}
