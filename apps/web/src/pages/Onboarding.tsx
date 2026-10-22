import { useNavigate } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
import { Check, Copy, ExternalLink } from 'lucide-react'
import type { FormEvent } from 'react'
import { useState } from 'react'

import type { Connection, ConnectionSetup } from '../api/types'
import { ApiError } from '../api/types'
import { rememberTenant, useApi, useCurrentTenantId, useMe } from '../app/context'
import { Alert, Button, Card, Field, Logo, StatusPill, cx } from '../components/ui'

export const COUNTRIES: [string, string][] = [
  ['NG', 'Nigeria'],
  ['KE', 'Kenya'],
  ['GH', 'Ghana'],
  ['ZA', 'South Africa'],
  ['EG', 'Egypt'],
  ['RW', 'Rwanda'],
  ['UG', 'Uganda'],
  ['TZ', 'Tanzania'],
  ['GB', 'United Kingdom'],
  ['US', 'Other (USD)'],
]

const STEPS = ['Workspace', 'Connect AWS', 'Deploy the stack', 'Check access']

function Progress({ step }: { step: number }) {
  return (
    <ol className="mb-6 grid grid-cols-4 gap-2" aria-label="Setup progress">
      {STEPS.map((label, i) => (
        <li key={label} aria-current={i === step ? 'step' : undefined}>
          <div className={cx('h-1.5 rounded-full', i <= step ? 'bg-brand-yellow' : 'bg-surface-2')} />
          <span className={cx('mt-1 block text-xs', i === step ? 'font-medium text-primary' : 'text-muted')}>
            {i + 1}. {label}
          </span>
        </li>
      ))}
    </ol>
  )
}

function CopyValue({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-medium text-secondary">{label}</span>
      <div className="flex items-center gap-2">
        <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-2 px-3 py-2 font-mono text-xs">{value}</code>
        <Button
          variant="secondary"
          aria-label={`Copy ${label}`}
          onClick={() => {
            void navigator.clipboard?.writeText(value).then(() => setCopied(true))
          }}
        >
          {copied ? <Check size={16} /> : <Copy size={16} />}
        </Button>
      </div>
    </div>
  )
}

export function Onboarding({ startAt = 0 }: { startAt?: number }) {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { data: me } = useMe()
  const [currentTenant] = useCurrentTenantId(me)
  const [step, setStep] = useState(startAt)
  const [tenantId, setTenantId] = useState<string | null>(startAt > 0 ? currentTenant : null)
  const [setup, setSetup] = useState<ConnectionSetup | null>(null)
  const [result, setResult] = useState<Connection | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)

  // Step 1
  const [name, setName] = useState('')
  const [country, setCountry] = useState('NG')
  // Step 2
  const [accountId, setAccountId] = useState('')
  const [accountType, setAccountType] = useState<'management' | 'standalone'>('management')
  const [createExport, setCreateExport] = useState(true)

  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    setFieldErrors({})
    try {
      await action()
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
        setFieldErrors(Object.fromEntries(err.fieldErrors.map((f) => [f.field, f.message])))
      } else {
        setError('Something went wrong. Try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  const createWorkspace = (e: FormEvent) => {
    e.preventDefault()
    void run(async () => {
      const tenant = await api.createTenant({ name, countryCode: country })
      rememberTenant(tenant.id)
      setTenantId(tenant.id)
      await qc.invalidateQueries({ queryKey: ['me'] })
      setStep(1)
    })
  }

  const connect = (e: FormEvent) => {
    e.preventDefault()
    if (!tenantId) return
    void run(async () => {
      setSetup(
        await api.createConnection(tenantId, {
          awsAccountId: accountId.replace(/\D/g, ''),
          accountType,
          createCurExport: createExport,
        }),
      )
      setStep(2)
    })
  }

  const check = () => {
    if (!tenantId || !setup) return
    setStep(3)
    void run(async () => {
      setResult(await api.validateConnection(tenantId, setup.connection.id))
      await qc.invalidateQueries({ queryKey: ['connections'] })
    })
  }

  return (
    <main className="mx-auto max-w-2xl px-4 py-10">
      <div className="mb-6 flex items-center gap-2 text-lg font-semibold">
        <Logo /> Cloudoptics
      </div>
      <Progress step={step} />

      {step === 0 && (
        <Card>
          <h1 className="text-2xl font-semibold">Create your workspace</h1>
          <p className="mt-1 text-sm text-secondary">
            Your country sets the local currency and time zone. You can change both later.
          </p>
          <form className="mt-6 flex flex-col gap-4" onSubmit={createWorkspace}>
            <Field
              label="Company or team name"
              required
              maxLength={120}
              value={name}
              onChange={(e) => setName(e.target.value)}
              error={fieldErrors.name}
            />
            <div className="flex flex-col gap-1">
              <label htmlFor="country" className="text-sm font-medium">
                Country
              </label>
              <select
                id="country"
                className="h-9 rounded-sm border border-border-strong bg-surface-1 px-3 text-sm"
                value={country}
                onChange={(e) => setCountry(e.target.value)}
              >
                {COUNTRIES.map(([code, label]) => (
                  <option key={code} value={code}>
                    {label}
                  </option>
                ))}
              </select>
            </div>
            {error && <Alert tone="error">{error}</Alert>}
            <Button type="submit" disabled={busy}>
              {busy ? 'Creating…' : 'Create workspace'}
            </Button>
          </form>
        </Card>
      )}

      {step === 1 && (
        <Card>
          <h1 className="text-2xl font-semibold">Connect your AWS account</h1>
          <p className="mt-1 text-sm text-secondary">
            Cloudoptics only reads billing data. It never gets permission to change anything in your
            account.
          </p>
          <form className="mt-6 flex flex-col gap-4" onSubmit={connect}>
            <Field
              label="AWS account ID"
              inputMode="numeric"
              placeholder="123456789012"
              required
              value={accountId}
              onChange={(e) => setAccountId(e.target.value)}
              hint="12 digits. Use the management (payer) account to see every account in your organisation."
              error={fieldErrors.awsAccountId}
            />
            <fieldset className="flex flex-col gap-2">
              <legend className="text-sm font-medium">Account type</legend>
              {(
                [
                  ['management', 'Management (payer) account of an AWS Organization'],
                  ['standalone', 'Standalone account'],
                ] as const
              ).map(([value, label]) => (
                <label key={value} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="accountType"
                    checked={accountType === value}
                    onChange={() => setAccountType(value)}
                  />
                  {label}
                </label>
              ))}
            </fieldset>
            <label className="flex items-start gap-2 text-sm">
              <input
                type="checkbox"
                className="mt-1"
                checked={createExport}
                onChange={(e) => setCreateExport(e.target.checked)}
              />
              <span>
                Create a Cost and Usage Report (CUR 2.0) export for Cloudoptics
                <span className="block text-xs text-muted">
                  Recommended. AWS charges only for the S3 storage, usually a few cents a month.
                </span>
              </span>
            </label>
            {error && <Alert tone="error">{error}</Alert>}
            <Button type="submit" disabled={busy}>
              {busy ? 'Preparing…' : 'Continue'}
            </Button>
          </form>
        </Card>
      )}

      {step === 2 && setup && (
        <Card>
          <h1 className="text-2xl font-semibold">Deploy the read-only role</h1>
          <ol className="mt-4 list-decimal space-y-2 pl-5 text-sm text-secondary">
            <li>Sign in to AWS account {setup.connection.awsAccountId} as an administrator.</li>
            <li>
              Open the CloudFormation link below. It opens in <strong>us-east-1</strong> with the
              values filled in.
            </li>
            <li>Tick the IAM acknowledgement box and choose <strong>Create stack</strong>.</li>
            <li>Wait for CREATE_COMPLETE (about 2 minutes), then come back here.</li>
          </ol>
          <div className="mt-6 flex flex-col gap-4">
            <a
              href={setup.quickCreateUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex h-9 items-center justify-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover"
            >
              Open CloudFormation in AWS <ExternalLink size={16} />
            </a>
            <CopyValue label="External ID" value={setup.externalId} />
            <details className="text-sm">
              <summary className="cursor-pointer text-secondary">Set it up by hand instead</summary>
              <div className="mt-3 flex flex-col gap-3">
                <CopyValue label="Template URL" value={setup.templateUrl} />
                <CopyValue label="Cloudoptics principal" value={setup.principalArn} />
                <CopyValue label="Role we will assume" value={setup.connection.roleArn} />
              </div>
            </details>
          </div>
          <div className="mt-6 flex justify-end">
            <Button onClick={check}>I have deployed the stack</Button>
          </div>
        </Card>
      )}

      {step === 3 && (
        <Card>
          <h1 className="text-2xl font-semibold">Checking access</h1>
          {busy && <p className="mt-2 text-sm text-secondary">Testing each permission…</p>}
          {error && (
            <div className="mt-4">
              <Alert tone="error">{error}</Alert>
            </div>
          )}
          {result && (
            <>
              <div className="mt-2 flex items-center gap-2 text-sm">
                Connection status: <StatusPill status={result.status} />
              </div>
              <ul className="mt-4 divide-y divide-border">
                {result.permissionChecks.map((c) => (
                  <li key={c.id} className="flex items-start justify-between gap-4 py-3">
                    <div>
                      <p className="text-sm font-medium">{c.label}</p>
                      <p className="text-sm text-secondary">{c.message}</p>
                      {c.hint && <p className="text-xs text-muted">{c.hint}</p>}
                    </div>
                    <StatusPill status={c.status} />
                  </li>
                ))}
              </ul>
              <div className="mt-6 flex justify-end gap-2">
                {result.status !== 'healthy' && (
                  <Button variant="secondary" onClick={check} disabled={busy}>
                    Check again
                  </Button>
                )}
                <Button onClick={() => void navigate({ to: '/overview' })}>Go to my workspace</Button>
              </div>
            </>
          )}
        </Card>
      )}
    </main>
  )
}
