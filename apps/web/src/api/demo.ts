import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type {
  Api,
  AuthConfig,
  Connection,
  ConnectionCreate,
  ConnectionSetup,
  CostSummary,
  Me,
  PermissionCheck,
  Tenant,
} from './types'
import { ApiError } from './types'

// In-browser stand-in for the API, used by the public GitHub Pages build. Nothing leaves
// the browser and no AWS call is made; state lives in this viewer's localStorage only.

const KEY = 'co.demo.v1'
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
}

const empty = (): State => ({ me: null, tenants: [], connections: {}, externalIds: {} })

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
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
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
    return { mode: 'dev', configured: true, provider: null, issuer: null, clientId: null }
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
    me.memberships.push({ tenantId: tenant.id, tenantName: name, role: 'admin' })
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
        { field: 'awsAccountId', message: 'An AWS account ID has exactly 12 digits' },
      ])
    }
    const all = Object.values(this.state.connections).flat()
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

  async getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup> {
    await this.tick()
    return this.setup(this.connection(tenantId, connectionId))
  }

  async validateConnection(tenantId: string, connectionId: string): Promise<Connection> {
    await delay(this.latencyMs * 4)
    const conn = this.connection(tenantId, connectionId)
    const bucket = `cloudoptics-cur-${conn.awsAccountId}`
    const checks: PermissionCheck[] = [
      { id: 'assume_role', label: 'Assume the Cloudoptics role', status: 'ok', message: 'Role assumed.', hint: null },
      { id: 'cost_explorer', label: 'Read Cost Explorer', status: 'ok', message: 'Cost data readable.', hint: null },
      { id: 'cur_export', label: 'Find the CUR 2.0 export', status: 'ok', message: 'Found export cloudoptics-cur2.', hint: null },
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
      { id: 'savings_plans', label: 'Read Savings Plans', status: 'ok', message: 'Savings Plans readable.', hint: null },
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
      month,
      currency: 'USD',
      dataThrough: null,
      lastLoadAt: null,
      lastLoadStatus: null,
      monthToDate: null,
      forecast: null,
      previousMonth: null,
    }
    if (!connected.some((c) => c.status === 'healthy' || c.status === 'degraded')) return empty
    // Illustrative figures in the shape of tools/cur-fixture: about USD 41 a day.
    const days = Math.max(now.getUTCDate() - 1, 1)
    const daysInMonth = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).getUTCDate()
    const daily = 41.26
    const through = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), days))
    return {
      ...empty,
      status: 'ready',
      dataThrough: through.toISOString().slice(0, 10),
      lastLoadAt: now.toISOString(),
      lastLoadStatus: 'succeeded',
      monthToDate: { effective: daily * days, billed: daily * days, credits: -2 * days },
      forecast: { amount: daily * daysInMonth, method: 'run_rate', daysCovered: days, daysInMonth },
      previousMonth: 1237.75,
    }
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
