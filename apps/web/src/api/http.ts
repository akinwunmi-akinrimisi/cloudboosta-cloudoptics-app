import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type {
  AccountList,
  AlertSettings,
  AlertSettingsView,
  Anomaly,
  AnomalyDetail,
  AnomalyStatus,
  DigestPreview,
  NotificationChoices,
  NotificationPreferences,
  Api,
  AuditPage,
  AuthConfig,
  AuthProvider,
  Connection,
  ConnectionCreate,
  ConnectionIntent,
  ConnectionSetup,
  IntentSetup,
  CostBreakdown,
  CostDimension,
  CostSummary,
  CurrentRate,
  DailyCosts,
  Me,
  InventorySummary,
  Invite,
  Budget,
  BudgetInput,
  Member,
  Recommendation,
  RecommendationChange,
  RecommendationEvent,
  RecommendationQuery,
  RecommendationSummary,
  RuleSettings,
  RuleSettingsView,
  TenantChange,
  UsageSeries,
  ResourceScan,
  Role,
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

  updateTenant(tenantId: string, change: TenantChange): Promise<Tenant> {
    return this.request('PATCH', `/v1/tenants/${tenantId}`, change)
  }

  listMembers(tenantId: string): Promise<Member[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/members`)
  }

  listBudgets(tenantId: string): Promise<Budget[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/budgets`)
  }

  createBudget(tenantId: string, body: BudgetInput): Promise<Budget> {
    return this.request('POST', `/v1/tenants/${tenantId}/budgets`, body)
  }

  updateBudget(tenantId: string, id: string, change: Partial<BudgetInput> & { isActive?: boolean }): Promise<Budget> {
    return this.request('PATCH', `/v1/tenants/${tenantId}/budgets/${id}`, change)
  }

  deleteBudget(tenantId: string, id: string): Promise<void> {
    return this.request('DELETE', `/v1/tenants/${tenantId}/budgets/${id}`)
  }

  listAnomalies(tenantId: string, status?: AnomalyStatus): Promise<Anomaly[]> {
    const query = status ? `?status=${status}` : ''
    return this.request('GET', `/v1/tenants/${tenantId}/anomalies${query}`)
  }

  getAnomaly(tenantId: string, id: string): Promise<AnomalyDetail> {
    return this.request('GET', `/v1/tenants/${tenantId}/anomalies/${id}`)
  }

  updateAnomaly(tenantId: string, id: string, change: { status: AnomalyStatus; note?: string | null }): Promise<AnomalyDetail> {
    return this.request('PATCH', `/v1/tenants/${tenantId}/anomalies/${id}`, change)
  }

  getNotificationPreferences(tenantId: string): Promise<NotificationPreferences> {
    return this.request('GET', `/v1/tenants/${tenantId}/notification-preferences`)
  }

  saveMyNotifications(tenantId: string, change: Partial<NotificationChoices>): Promise<NotificationPreferences> {
    return this.request('PUT', `/v1/tenants/${tenantId}/notification-preferences/me`, change)
  }

  saveNotificationDefaults(tenantId: string, change: Partial<NotificationChoices>): Promise<NotificationPreferences> {
    return this.request('PUT', `/v1/tenants/${tenantId}/notification-preferences/defaults`, change)
  }

  digestPreview(tenantId: string): Promise<DigestPreview> {
    return this.request('GET', `/v1/tenants/${tenantId}/digest/preview`)
  }

  getAlertSettings(tenantId: string): Promise<AlertSettingsView> {
    return this.request('GET', `/v1/tenants/${tenantId}/alert-settings`)
  }

  saveAlertSettings(tenantId: string, settings: AlertSettings): Promise<AlertSettingsView> {
    return this.request('PUT', `/v1/tenants/${tenantId}/alert-settings`, settings)
  }

  inviteMember(tenantId: string, email: string, role: Role): Promise<Invite> {
    return this.request('POST', `/v1/tenants/${tenantId}/invites`, { email, role })
  }

  listInvites(tenantId: string): Promise<Invite[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/invites`)
  }

  revokeInvite(tenantId: string, inviteId: string): Promise<void> {
    return this.request('DELETE', `/v1/tenants/${tenantId}/invites/${inviteId}`)
  }

  changeRole(tenantId: string, userId: string, role: Role): Promise<void> {
    return this.request('PATCH', `/v1/tenants/${tenantId}/members/${userId}`, { role })
  }

  removeMember(tenantId: string, userId: string): Promise<void> {
    return this.request('DELETE', `/v1/tenants/${tenantId}/members/${userId}`)
  }

  listConnections(tenantId: string): Promise<Connection[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/connections`)
  }

  createConnection(tenantId: string, body: ConnectionCreate): Promise<ConnectionSetup> {
    return this.request('POST', `/v1/tenants/${tenantId}/connections`, body)
  }

  createIntent(tenantId: string): Promise<IntentSetup> {
    return this.request('POST', `/v1/tenants/${tenantId}/connections/intents`)
  }

  getIntent(tenantId: string, intentId: string): Promise<ConnectionIntent> {
    return this.request('GET', `/v1/tenants/${tenantId}/connections/intents/${intentId}`)
  }

  getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup> {
    return this.request('GET', `/v1/tenants/${tenantId}/connections/${connectionId}/setup`)
  }

  costSummary(tenantId: string): Promise<CostSummary> {
    return this.request('GET', `/v1/tenants/${tenantId}/costs/summary`)
  }

  costDaily(tenantId: string, days = 30): Promise<DailyCosts> {
    return this.request('GET', `/v1/tenants/${tenantId}/costs/daily?days=${days}`)
  }

  costBreakdown(tenantId: string, dimension: CostDimension, limit = 8): Promise<CostBreakdown> {
    return this.request('GET', `/v1/tenants/${tenantId}/costs/breakdown?dimension=${dimension}&limit=${limit}`)
  }

  listRecommendations(tenantId: string, query: RecommendationQuery = {}): Promise<Recommendation[]> {
    const params = new URLSearchParams({ status: query.status ?? 'open' })
    if (query.alternatives) params.set('alternatives', 'true')
    return this.request('GET', `/v1/tenants/${tenantId}/recommendations?${params.toString()}`)
  }

  recommendationSummary(tenantId: string): Promise<RecommendationSummary> {
    return this.request('GET', `/v1/tenants/${tenantId}/recommendations/summary`)
  }

  getRecommendation(tenantId: string, id: string): Promise<Recommendation> {
    return this.request('GET', `/v1/tenants/${tenantId}/recommendations/${id}`)
  }

  updateRecommendation(tenantId: string, id: string, change: RecommendationChange): Promise<Recommendation> {
    return this.request('PATCH', `/v1/tenants/${tenantId}/recommendations/${id}`, change)
  }

  updateRecommendations(tenantId: string, ids: string[], change: RecommendationChange): Promise<{ updated: number }> {
    return this.request('POST', `/v1/tenants/${tenantId}/recommendations/bulk`, { ids, ...change })
  }

  recommendationEvents(tenantId: string, id: string): Promise<RecommendationEvent[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/recommendations/${id}/events`)
  }

  recommendationUsage(tenantId: string, id: string): Promise<UsageSeries[]> {
    return this.request('GET', `/v1/tenants/${tenantId}/recommendations/${id}/usage`)
  }

  addComment(tenantId: string, id: string, body: string): Promise<RecommendationEvent[]> {
    return this.request('POST', `/v1/tenants/${tenantId}/recommendations/${id}/comments`, { body })
  }

  getRuleSettings(tenantId: string): Promise<RuleSettingsView> {
    return this.request('GET', `/v1/tenants/${tenantId}/rule-settings`)
  }

  saveRuleSettings(tenantId: string, settings: RuleSettings): Promise<RuleSettingsView> {
    return this.request('PUT', `/v1/tenants/${tenantId}/rule-settings`, settings)
  }

  auditLog(tenantId: string, before?: number): Promise<AuditPage> {
    const query = before === undefined ? '' : `?before=${before}`
    return this.request('GET', `/v1/tenants/${tenantId}/audit-log${query}`)
  }

  listAccounts(tenantId: string): Promise<AccountList> {
    return this.request('GET', `/v1/tenants/${tenantId}/accounts`)
  }

  fxCurrent(tenantId: string): Promise<CurrentRate> {
    return this.request('GET', `/v1/tenants/${tenantId}/fx/current`)
  }

  disconnectConnection(tenantId: string, connectionId: string): Promise<void> {
    return this.request('DELETE', `/v1/tenants/${tenantId}/connections/${connectionId}`)
  }

  scanResources(tenantId: string, connectionId: string): Promise<ResourceScan> {
    return this.request('POST', `/v1/tenants/${tenantId}/connections/${connectionId}/scan`)
  }

  resourceSummary(tenantId: string): Promise<InventorySummary> {
    return this.request('GET', `/v1/tenants/${tenantId}/resources/summary`)
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
