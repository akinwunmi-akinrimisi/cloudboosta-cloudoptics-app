import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type {
  Api,
  AuthConfig,
  AuthProvider,
  Connection,
  ConnectionCreate,
  ConnectionSetup,
  CostSummary,
  Me,
  Tenant,
} from './types'
import { ApiError } from './types'

const TOKEN_KEY = 'co.token'

// Talks to the Cloudoptics API. Signs in through WorkOS AuthKit when the API says so
// (/v1/auth/config, ADR-006); otherwise uses the development token endpoint.
export class HttpApi implements Api {
  readonly mode = 'http' as const
  private readonly baseUrl: string
  private provider: AuthProvider | null = null

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, '')
  }

  authConfig(): Promise<AuthConfig> {
    return this.request('GET', '/v1/auth/config', undefined, false)
  }

  /** Use an identity provider session for tokens, sign-in and sign-out. */
  attachProvider(provider: AuthProvider | null): void {
    this.provider = provider
  }

  isSignedIn(): boolean {
    return this.provider ? this.provider.signedIn : read('session', TOKEN_KEY) !== null
  }

  async signIn(email: string, name?: string): Promise<void> {
    if (this.provider) return this.provider.signIn()
    const res = await this.request<{ accessToken: string }>('POST', '/v1/dev/token', { email, name }, false)
    write('session', TOKEN_KEY, res.accessToken)
  }

  signOut(): void {
    write('session', TOKEN_KEY, null)
    this.provider?.signOut()
  }

  me(): Promise<Me> {
    return this.request('GET', '/v1/me')
  }

  updateMe(view: CurrencyMode): Promise<Me> {
    return this.request('PATCH', '/v1/me', { preferredCurrencyView: view })
  }

  createTenant(body: { name: string; countryCode: string }): Promise<Tenant> {
    return this.request('POST', '/v1/tenants', body)
  }

  getTenant(tenantId: string): Promise<Tenant> {
    return this.request('GET', `/v1/tenants/${tenantId}`)
  }

  listConnections(tenantId: string): Promise<Connection[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/connections`)
  }

  createConnection(tenantId: string, body: ConnectionCreate): Promise<ConnectionSetup> {
    return this.request('POST', `/v1/tenants/${tenantId}/connections`, body)
  }

  getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup> {
    return this.request('GET', `/v1/tenants/${tenantId}/connections/${connectionId}/setup`)
  }

  costSummary(tenantId: string): Promise<CostSummary> {
    return this.request('GET', `/v1/tenants/${tenantId}/costs/summary`)
  }

  async validateConnection(tenantId: string, connectionId: string): Promise<Connection> {
    const path = `/v1/tenants/${tenantId}/connections/${connectionId}`
    const result = await this.request<Connection | { status: 'validating' }>('POST', `${path}/validate`)
    if ('id' in result) return result
    // Queued: poll until the worker finishes (staging/production).
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 2000))
      const conn = await this.request<Connection>('GET', path)
      if (conn.status !== 'validating') return conn
    }
    throw new ApiError(504, 'Validation is taking longer than expected. Check back in a minute.')
  }

  private async request<T>(method: string, path: string, body?: unknown, auth = true): Promise<T> {
    const headers: Record<string, string> = { accept: 'application/json' }
    if (body !== undefined) headers['content-type'] = 'application/json'
    const token = auth ? (this.provider ? await this.provider.getToken() : read('session', TOKEN_KEY)) : null
    if (token) headers.authorization = `Bearer ${token}`
    let res: Response
    try {
      res = await fetch(`${this.baseUrl}${path}`, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      })
    } catch {
      throw new ApiError(0, 'Cannot reach the Cloudoptics API. Check your connection and try again.')
    }
    if (res.status === 401 && !this.provider) this.signOut()
    if (!res.ok) {
      const problem = (await res.json().catch(() => ({}))) as {
        title?: string
        errors?: { field: string; message: string }[]
      }
      throw new ApiError(res.status, problem.title ?? `Request failed (${res.status})`, problem.errors)
    }
    return (res.status === 204 ? undefined : await res.json()) as T
  }
}
