import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type { Api, Connection, ConnectionCreate, ConnectionSetup, Me, Tenant } from './types'
import { ApiError } from './types'

const TOKEN_KEY = 'co.token'

// Talks to the Cloudoptics API. Sign-in uses the dev token endpoint for now; the OIDC
// provider (ADR-006) replaces it after the Sprint 0 spike, behind the same interface.
export class HttpApi implements Api {
  readonly mode = 'http' as const
  private readonly baseUrl: string

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl.replace(/\/$/, '')
  }

  isSignedIn(): boolean {
    return read('session', TOKEN_KEY) !== null
  }

  async signIn(email: string, name?: string): Promise<void> {
    const res = await this.request<{ accessToken: string }>('POST', '/v1/dev/token', { email, name }, false)
    write('session', TOKEN_KEY, res.accessToken)
  }

  signOut(): void {
    write('session', TOKEN_KEY, null)
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
    const token = read('session', TOKEN_KEY)
    if (auth && token) headers.authorization = `Bearer ${token}`
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
    if (res.status === 401) this.signOut()
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
