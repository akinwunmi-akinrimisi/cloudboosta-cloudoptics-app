import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type {
  Api,
  Connection,
  ConnectionCreate,
  ConnectionSetup,
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

  private connection(tenantId: string, connectionId: string): Connection {
    this.tenant(tenantId)
    const conn = (this.state.connections[tenantId] ?? []).find((c) => c.id === connectionId)
    if (!conn) throw new ApiError(404, 'Connection not found')
    return conn
  }

  private setup(conn: Connection): ConnectionSetup {
    const template = 'https://cloudoptics-public.s3.eu-west-2.amazonaws.com/cfn/v1/billing-role.yaml'
    const principal = 'arn:aws:iam::000000000000:role/CloudopticsBroker'
    const ext = this.state.externalIds[conn.id] ?? ''
    const params = new URLSearchParams({
      templateURL: template,
      stackName: 'cloudoptics-billing-access',
      param_ExternalId: ext,
      param_CloudopticsPrincipalArn: principal,
      param_CreateCurExport: String(conn.createCurExport),
    })
    return {
      connection: structuredClone(conn),
      externalId: ext,
      quickCreateUrl: `https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?${params}`,
      templateUrl: template,
      resourceTemplateUrl: template.replace('billing-role', 'resource-role'),
      stackRegion: 'us-east-1',
      principalArn: principal,
    }
  }
}
