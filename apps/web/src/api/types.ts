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

export type ConnectionStatus =
  | 'pending'
  | 'validating'
  | 'healthy'
  | 'degraded'
  | 'failed'
  | 'disconnected'

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
  quickCreateUrl: string
  templateUrl: string
  resourceTemplateUrl: string
  stackRegion: string
  principalArn: string
}

export interface ConnectionCreate {
  awsAccountId: string
  accountType: 'management' | 'standalone'
  createCurExport: boolean
}

export interface Api {
  readonly mode: 'http' | 'demo'
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
