import { randomBytes, randomInt } from 'node:crypto'

import type { APIRequestContext, Page } from '@playwright/test'
import { expect } from '@playwright/test'

/** Helpers that talk to the API directly: for setting up state a test is not about. */

export const API_URL = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000'
/** Cloudoptics staff in the E2E stack (CLOUDOPTICS_PLATFORM_ADMIN_EMAILS in server/serve.py). */
export const STAFF_EMAIL = 'fx-staff@example.com'
/** The reference NGN rate global.setup.ts sets. */
export const NGN_RATE = 1550

export function uniqueEmail(prefix: string): string {
  return `${prefix}-${randomBytes(4).toString('hex')}@example.com`
}

export function accountId(): string {
  return Array.from({ length: 12 }, () => randomInt(10)).join('')
}

export class Api {
  constructor(
    private readonly request: APIRequestContext,
    readonly token: string,
  ) {}

  private headers() {
    return { authorization: `Bearer ${this.token}` }
  }

  async get<T = unknown>(path: string): Promise<T> {
    const r = await this.request.get(`${API_URL}${path}`, { headers: this.headers() })
    expect(r.status(), `GET ${path}: ${await r.text()}`).toBeLessThan(300)
    return (await r.json()) as T
  }

  async send<T = unknown>(method: 'POST' | 'PATCH' | 'DELETE', path: string, data?: unknown): Promise<T> {
    const r = await this.request.fetch(`${API_URL}${path}`, { method, headers: this.headers(), data })
    expect(r.status(), `${method} ${path}: ${await r.text()}`).toBeLessThan(300)
    return (r.status() === 204 ? undefined : await r.json()) as T
  }
}

export async function devToken(
  request: APIRequestContext,
  email: string,
  { mfa = false, name = 'E2E User' }: { mfa?: boolean; name?: string } = {},
): Promise<string> {
  const r = await request.post(`${API_URL}/v1/dev/token`, { data: { email, name, mfa } })
  expect(r.ok(), await r.text()).toBeTruthy()
  return ((await r.json()) as { accessToken: string }).accessToken
}

/** Signs in as a user (and joins any workspace they were invited to, as `GET /v1/me` does). */
export async function signInApi(
  request: APIRequestContext,
  email: string,
  options: { mfa?: boolean; name?: string } = {},
): Promise<Api> {
  const api = new Api(request, await devToken(request, email, options))
  await api.get('/v1/me')
  return api
}

export interface IntentSetup {
  intent: { id: string }
  externalId: string
  connectionRef: string
}

/** What the CloudFormation stack's setup step sends when it is created in `account`. */
export function stackCallback(
  tenantId: string,
  setup: { externalId: string; connectionRef: string },
  account: string,
  requestType: 'Create' | 'Update' | 'Delete' = 'Create',
) {
  return {
    requestType,
    ref: setup.connectionRef,
    tenantId,
    externalId: setup.externalId,
    accountId: account,
    management: true,
    curBucket: `cloudoptics-cur-${account}`,
    curPrefix: 'cloudoptics',
    stackName: 'cloudoptics-access',
    templateVersion: '2.0.4',
  }
}

export async function sendStackCallback(request: APIRequestContext, body: ReturnType<typeof stackCallback>) {
  const r = await request.post(`${API_URL}/v1/cfn/callback`, { data: body })
  expect(r.ok(), await r.text()).toBeTruthy()
  return (await r.json()) as { connectionId: string; status: string }
}

export interface Workspace {
  api: Api
  email: string
  name: string
  tenantId: string
  connectionId: string
  account: string
}

/**
 * A workspace with a healthy connection, its bills loaded and its resources scanned and
 * analysed: what an admin has a day after connecting. Everything goes through the API
 * (the stack callback included); only AWS is fake.
 */
export async function seedWorkspace(
  request: APIRequestContext,
  { prefix = 'admin', bills = true, scan = true, mfa = false }: { prefix?: string; bills?: boolean; scan?: boolean; mfa?: boolean } = {},
): Promise<Workspace> {
  const email = uniqueEmail(prefix)
  const api = await signInApi(request, email, { mfa, name: 'Ada Admin' })
  const name = `E2E ${randomBytes(3).toString('hex')}`
  const tenant = await api.send<{ id: string }>('POST', '/v1/tenants', { name, countryCode: 'NG' })
  const setup = await api.send<IntentSetup>('POST', `/v1/tenants/${tenant.id}/connections/intents`)
  const account = accountId()
  const { connectionId } = await sendStackCallback(request, stackCallback(tenant.id, setup, account))
  const base = `/v1/tenants/${tenant.id}/connections/${connectionId}`
  if (bills) {
    // Production's worker polls the bucket; here the admin-only "load now" route stands in for it.
    const r = await api.send<{ status: string }>('POST', `${base}/ingest`)
    expect(r.status).toBe('succeeded')
  }
  if (scan) await api.send('POST', `${base}/scan`)
  return { api, email, name, tenantId: tenant.id, connectionId, account }
}

/** Adds a teammate with a role, through an invite they accept by signing in. */
export async function addTeammate(
  request: APIRequestContext,
  ws: Workspace,
  role: 'admin' | 'member' | 'viewer',
  options: { mfa?: boolean } = {},
): Promise<{ api: Api; email: string; userId: string }> {
  const email = uniqueEmail(role)
  await ws.api.send('POST', `/v1/tenants/${ws.tenantId}/invites`, { email, role })
  const api = await signInApi(request, email, { ...options, name: `Tess ${role}` })
  const members = await ws.api.get<{ userId: string; email: string }[]>(`/v1/tenants/${ws.tenantId}/members`)
  const userId = members.find((m) => m.email === email)?.userId
  expect(userId, `${email} joined`).toBeTruthy()
  return { api, email, userId: userId as string }
}

/** Opens the app signed in with `token`, in workspace `tenantId` (the app's own storage keys). */
export async function openSignedIn(page: Page, token: string, tenantId?: string, path = '/overview') {
  await page.addInitScript(
    ([t, tenant]) => {
      window.sessionStorage.setItem('co.token', t)
      if (tenant) window.localStorage.setItem('co.tenant', tenant)
    },
    [token, tenantId ?? ''] as const,
  )
  await page.goto(path)
}

export function usd(amount: number): string {
  return new Intl.NumberFormat('en', { style: 'currency', currency: 'USD', currencyDisplay: 'narrowSymbol' }).format(
    amount,
  )
}

export function ngn(amount: number): string {
  const value = Math.sign(amount) * (Math.round(Math.abs(amount * NGN_RATE) * 100) / 100)
  return new Intl.NumberFormat('en', { style: 'currency', currency: 'NGN', currencyDisplay: 'narrowSymbol' }).format(
    value,
  )
}
