import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, ExternalLink, KeyRound, ListChecks, ShieldCheck } from 'lucide-react'
import type { FormEvent, ReactNode } from 'react'
import { useState } from 'react'

import type { Connection } from '../../api/types'
import { ApiError } from '../../api/types'
import { useApi } from '../../app/context'
import { Alert, Button, Card, Field } from '../../components/ui'

/** DigitalOcean's page for a new personal access token. */
/** Account → API in DigitalOcean's control panel: the Tokens tab, with Generate New Token. */
export const DO_TOKEN_PAGE = 'https://cloud.digitalocean.com/account/api/tokens'
/** Needed for costs. */
export const DO_REQUIRED_SCOPES = ['account:read', 'billing:read']
/** Turn on the resource scan and its recommendations. */
export const DO_RESOURCE_SCOPES = [
  'droplet:read',
  'block_storage:read',
  'snapshot:read',
  'reserved_ip:read',
  'load_balancer:read',
  'database:read',
  'monitoring:read',
  'tag:read',
]
// Same shape as the API's check (providers/digitalocean/client.py TOKEN_PATTERN).
const TOKEN = /^(do[po]_v1_)?[a-f0-9]{64}$/

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong. Try again.'
}

function Step({ icon, children }: { icon: ReactNode; children: ReactNode }) {
  return (
    <li className="flex gap-3">
      <span className="mt-0.5 shrink-0 text-accent" aria-hidden="true">
        {icon}
      </span>
      <div className="min-w-0 text-sm text-secondary">{children}</div>
    </li>
  )
}

function Scopes({ names }: { names: string[] }) {
  return (
    <ul className="mt-1 flex flex-wrap gap-1">
      {names.map((s) => (
        <li key={s}>
          <code className="rounded-sm bg-surface-2 px-1.5 py-0.5 font-mono text-xs">{s}</code>
        </li>
      ))}
    </ul>
  )
}

/** The three customer steps: make a token, tick the read scopes, paste it. */
export function TokenSteps() {
  return (
    <ol className="mt-4 flex flex-col gap-3">
      <Step icon={<ExternalLink size={16} />}>
        In DigitalOcean, switch to the team that gets the bill, open{' '}
        <a className="text-accent underline" href={DO_TOKEN_PAGE} target="_blank" rel="noreferrer">
          Account → API
        </a>{' '}
        and choose <strong>Generate New Token</strong>.
      </Step>
      <Step icon={<ListChecks size={16} />}>
        Choose <strong>Custom Scopes</strong> and tick only these:
        <Scopes names={DO_REQUIRED_SCOPES} />
        <span className="mt-2 block">For recommendations, also:</span>
        <Scopes names={DO_RESOURCE_SCOPES} />
      </Step>
      <Step icon={<KeyRound size={16} />}>
        Pick an expiry, choose <strong>Generate Token</strong>, copy the token (it is shown once) and paste it below.
      </Step>
    </ol>
  )
}

function TokenField({
  label,
  value,
  onChange,
  error,
}: {
  label: string
  value: string
  onChange: (v: string) => void
  error?: string
}) {
  return (
    <Field
      label={label}
      type="password"
      autoComplete="off"
      spellCheck={false}
      placeholder="dop_v1_…"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      error={error}
      hint="Shown once by DigitalOcean. Cloudoptics stores it encrypted and never shows it again."
    />
  )
}

function formatError(token: string): string | undefined {
  const t = token.trim()
  return t && !TOKEN.test(t) ? 'Paste the whole token: it starts with dop_v1_.' : undefined
}

/** Onboarding, step 2 for DigitalOcean: paste a read-only token. */
export function DigitalOceanConnect({
  tenantId,
  onConnected,
}: {
  tenantId: string
  onConnected: (connectionId: string) => void
}) {
  const api = useApi()
  const qc = useQueryClient()
  const [token, setToken] = useState('')
  const [touched, setTouched] = useState(false)
  const connect = useMutation({
    mutationFn: (value: string) => api.connectDigitalOcean(tenantId, value),
    onSuccess: (conn: Connection) => {
      setToken('')
      void qc.invalidateQueries({ queryKey: ['connections'] })
      onConnected(conn.id)
    },
  })
  const submit = (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (formatError(token) || !token.trim()) return
    connect.mutate(token.trim())
  }
  return (
    <Card>
      <h1 className="text-2xl font-semibold">Connect DigitalOcean</h1>
      <p className="mt-1 flex items-start gap-2 text-sm text-secondary">
        <ShieldCheck size={16} className="mt-0.5 shrink-0" aria-hidden="true" />A read-only token lets Cloudoptics read
        your bill and resources. It cannot change anything.
      </p>
      <TokenSteps />
      <form className="mt-6 flex flex-col gap-4" onSubmit={submit}>
        <TokenField
          label="DigitalOcean token"
          value={token}
          onChange={setToken}
          error={touched ? formatError(token) : undefined}
        />
        {connect.error && <Alert tone="error">{message(connect.error)}</Alert>}
        <Button type="submit" disabled={connect.isPending || !token.trim()}>
          {connect.isPending ? 'Checking the token…' : 'Connect'}
        </Button>
      </form>
    </Card>
  )
}

/** Connections page: rotate a DigitalOcean token (the new one must read the same team). */
export function ReplaceDigitalOceanToken({ tenantId, connectionId }: { tenantId: string; connectionId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const [open, setOpen] = useState(false)
  const [token, setToken] = useState('')
  const replace = useMutation({
    mutationFn: (value: string) => api.replaceDigitalOceanToken(tenantId, connectionId, value),
    onSuccess: () => {
      setToken('')
      setOpen(false)
      void qc.invalidateQueries({ queryKey: ['connections', tenantId] })
    },
  })
  if (!open) {
    return (
      <div className="flex flex-col gap-2">
        {replace.isSuccess && (
          <p role="status" className="flex items-center gap-1 text-sm text-secondary">
            <Check size={16} aria-hidden="true" /> Token replaced. Delete the old one in DigitalOcean.
          </p>
        )}
        <div>
          <Button variant="secondary" onClick={() => setOpen(true)}>
            Replace token…
          </Button>
        </div>
      </div>
    )
  }
  return (
    <form
      className="flex flex-col gap-3 rounded-sm bg-surface-2 p-3"
      onSubmit={(e) => {
        e.preventDefault()
        if (!formatError(token) && token.trim()) replace.mutate(token.trim())
      }}
    >
      <p className="text-sm text-secondary">
        Make a new token with the same scopes{' '}
        <a className="text-accent underline" href={DO_TOKEN_PAGE} target="_blank" rel="noreferrer">
          in DigitalOcean
        </a>
        , paste it here, then delete the old one there.
      </p>
      <TokenField label="New DigitalOcean token" value={token} onChange={setToken} error={formatError(token)} />
      {replace.error && <Alert tone="error">{message(replace.error)}</Alert>}
      <div className="flex gap-2">
        <Button type="submit" disabled={replace.isPending || !token.trim()}>
          {replace.isPending ? 'Checking…' : 'Replace token'}
        </Button>
        <Button variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </form>
  )
}
