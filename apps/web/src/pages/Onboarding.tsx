import { Link, useNavigate } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, Download, ExternalLink } from 'lucide-react'
import type { FormEvent } from 'react'
import { useEffect, useState } from 'react'

import type { Connection, IntentSetup } from '../api/types'
import { ACCESS_STACK_NAME, ApiError } from '../api/types'
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

const STEPS = ['Workspace', 'Connect AWS', 'Check access']
const POLL_MS = 5000
// After this long with no word from the stack, say so and offer the link again.
const SLOW_MS = 10 * 60 * 1000

function Progress({ step }: { step: number }) {
  return (
    <ol className="mb-6 grid grid-cols-3 gap-2" aria-label="Setup progress">
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

export function CopyValue({ label, value }: { label: string; value: string }) {
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

const LINK_STYLE =
  'inline-flex h-9 items-center justify-center gap-2 rounded-sm bg-accent px-4 text-sm font-medium text-on-accent hover:bg-accent-hover'

/** The values for creating the stack by hand, when one-click links are off. */
function ByHand({
  templateUrl,
  values,
}: {
  templateUrl: string
  values: { label: string; value: string }[]
}) {
  return (
    <div className="flex flex-col gap-3">
      <ol className="list-decimal space-y-1 pl-5 text-sm text-secondary">
        <li>Download the template.</li>
        <li>
          In AWS, open{' '}
          <a
            className="text-accent underline"
            href="https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create"
            target="_blank"
            rel="noreferrer"
          >
            CloudFormation → Create stack
          </a>
          , choose <strong>Upload a template file</strong> and name the stack{' '}
          <code className="font-mono text-xs">{ACCESS_STACK_NAME}</code>.
        </li>
        <li>Copy the values below into the matching boxes, tick the acknowledgement and choose Submit.</li>
      </ol>
      <a href={templateUrl} download="cloudoptics-access.yaml" target="_blank" rel="noreferrer" className={LINK_STYLE}>
        Download template <Download size={16} />
      </a>
      {values.map((v) => (
        <CopyValue key={v.label} label={v.label} value={v.value} />
      ))}
    </div>
  )
}

function handValues(s: {
  externalId: string
  principalArn: string
  resourceTemplateUrl: string
  callbackUrl: string
  connectionRef: string
  tenantId: string
}) {
  return [
    { label: 'ExternalId', value: s.externalId },
    { label: 'CloudopticsPrincipalArn', value: s.principalArn },
    { label: 'ResourceTemplateUrl', value: s.resourceTemplateUrl },
    { label: 'CallbackUrl', value: s.callbackUrl },
    { label: 'ConnectionRef', value: s.connectionRef },
    { label: 'TenantId', value: s.tenantId },
  ]
}

/** What the person does in AWS: sign in, press one button, tick one box. */
function AwsSteps({ accountId }: { accountId?: string }) {
  return (
    <ol className="mt-4 list-decimal space-y-2 pl-5 text-sm text-secondary">
      <li>
        In this browser, sign in to{' '}
        {accountId ? (
          <>AWS account {accountId}</>
        ) : (
          <>your main AWS account, the one that receives the bill for all your accounts</>
        )}
        .
      </li>
      <li>Press the button below. AWS opens with everything filled in.</li>
      <li>
        At the bottom of that page, tick the box that starts “I acknowledge” and choose <strong>Create stack</strong>.
      </li>
      <li>Come back here. This page moves on by itself when AWS is done, in about 3 minutes.</li>
    </ol>
  )
}

function Waiting({ since, onRetry, error }: { since: number; onRetry: () => void; error: string | null }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), POLL_MS)
    return () => clearInterval(id)
  }, [])
  if (error) {
    return (
      <div className="mt-6 flex flex-col gap-3">
        <Alert tone="error">{error}</Alert>
        <div>
          <Button variant="secondary" onClick={onRetry}>
            Start again with a new link
          </Button>
        </div>
      </div>
    )
  }
  const slow = now - since > SLOW_MS
  return (
    <div className="mt-6 flex flex-col gap-2" role="status">
      <p className="text-sm font-medium">Waiting for AWS…</p>
      <p className="text-sm text-secondary">
        {slow
          ? 'Nothing from AWS yet. In CloudFormation, check that the cloudoptics-access stack exists and is not showing an error, or open the link again.'
          : 'AWS is creating read-only access for CloudOptics. Keep this page open.'}
      </p>
    </div>
  )
}

/** Shows the access check as it runs, then where to go next. */
function CheckAccess({ tenantId, connectionId }: { tenantId: string; connectionId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const list = useQuery({
    queryKey: ['connections', tenantId],
    queryFn: () => api.listConnections(tenantId),
    refetchInterval: (q) => {
      const c = q.state.data?.find((x) => x.id === connectionId)
      return !c || c.status === 'pending' || c.status === 'validating' ? 3000 : false
    },
  })
  const conn: Connection | undefined = list.data?.find((c) => c.id === connectionId)
  const running = !conn || conn.status === 'pending' || conn.status === 'validating' || checking

  const again = async () => {
    setChecking(true)
    setError(null)
    try {
      await api.validateConnection(tenantId, connectionId)
      await qc.invalidateQueries({ queryKey: ['connections', tenantId] })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.')
    } finally {
      setChecking(false)
    }
  }

  return (
    <Card>
      <h1 className="text-2xl font-semibold">
        {running ? 'Checking access' : conn?.status === 'healthy' ? 'Connected' : 'Almost there'}
      </h1>
      {conn && (
        <p className="mt-1 text-sm text-secondary">
          AWS account {conn.awsAccountId}
          {conn.accountType === 'management' ? ', with every account in your organization' : ''}.
        </p>
      )}
      {running && <p className="mt-2 text-sm text-secondary">Testing each permission…</p>}
      {error && (
        <div className="mt-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      {conn && !running && (
        <>
          <div className="mt-2 flex items-center gap-2 text-sm">
            Connection status: <StatusPill status={conn.status} />
          </div>
          <ul className="mt-4 divide-y divide-border">
            {conn.permissionChecks.map((c) => (
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
          {conn.status === 'healthy' && (
            <p className="mt-4 text-sm text-secondary">
              Your first costs appear within a day, when AWS delivers the first bill. Recommendations follow the first
              resource scan, a few minutes from now.
            </p>
          )}
          <div className="mt-6 flex justify-end gap-2">
            {conn.status !== 'healthy' && (
              <Button variant="secondary" onClick={() => void again()} disabled={checking}>
                Check again
              </Button>
            )}
            <Button onClick={() => void navigate({ to: '/overview' })}>Go to my workspace</Button>
          </div>
        </>
      )}
    </Card>
  )
}

/** New account: one link, no form; the stack tells CloudOptics which account it is in. */
function ConnectNew({ tenantId, onConnected }: { tenantId: string; onConnected: (connectionId: string) => void }) {
  const api = useApi()
  const [attempt, setAttempt] = useState(0)
  const [openedAt, setOpenedAt] = useState<number | null>(null)
  // One link per attempt; "Start again" makes a fresh one.
  const link = useQuery({
    queryKey: ['intent-setup', tenantId, attempt],
    queryFn: () => api.createIntent(tenantId),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: false,
  })
  const setup: IntentSetup | undefined = link.data
  const error = link.error
    ? link.error instanceof ApiError
      ? link.error.message
      : 'Could not prepare the link. Try again.'
    : null
  const start = () => {
    setOpenedAt(null)
    setAttempt((n) => n + 1)
  }

  const intent = useQuery({
    queryKey: ['intent', tenantId, setup?.intent.id],
    queryFn: () => api.getIntent(tenantId, (setup as IntentSetup).intent.id),
    enabled: Boolean(setup && openedAt !== null),
    refetchInterval: (q) => (q.state.data?.status === 'connected' || q.state.data?.error ? false : POLL_MS),
  })
  const connectedId = intent.data?.connectionId
  useEffect(() => {
    if (connectedId) onConnected(connectedId)
  }, [connectedId, onConnected])

  return (
    <Card>
      <h1 className="text-2xl font-semibold">Connect your AWS account</h1>
      <p className="mt-1 text-sm text-secondary">
        One click in AWS gives CloudOptics read-only access to your bill and your resources. It can never change
        anything in your account.
      </p>
      <AwsSteps />
      {error && (
        <div className="mt-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      {!setup && !error && <p className="mt-6 text-sm text-secondary">Preparing your link…</p>}
      {setup && (
        <div className="mt-6 flex flex-col gap-4">
          {setup.quickCreateUrl ? (
            <a
              href={setup.quickCreateUrl}
              target="_blank"
              rel="noreferrer"
              className={LINK_STYLE}
              onClick={() => setOpenedAt((t) => t ?? Date.now())}
            >
              Open AWS and connect <ExternalLink size={16} />
            </a>
          ) : (
            <>
              <ByHand
                templateUrl={setup.templateDownloadUrl}
                values={handValues({ ...setup, tenantId })}
              />
              {openedAt === null && (
                <div className="flex justify-end">
                  <Button onClick={() => setOpenedAt(Date.now())}>I have created the stack</Button>
                </div>
              )}
            </>
          )}
          {openedAt !== null && <Waiting since={openedAt} onRetry={start} error={intent.data?.error ?? null} />}
        </div>
      )}
    </Card>
  )
}

/** An existing connection: move it to the single stack, or set it up again. */
function ConnectExisting({
  tenantId,
  connectionId,
  onConnected,
}: {
  tenantId: string
  connectionId: string
  onConnected: () => void
}) {
  const api = useApi()
  const [openedAt, setOpenedAt] = useState<number | null>(null)
  const setup = useQuery({
    queryKey: ['setup', tenantId, connectionId],
    queryFn: () => api.getSetup(tenantId, connectionId),
  })
  const list = useQuery({
    queryKey: ['connections', tenantId],
    queryFn: () => api.listConnections(tenantId),
    enabled: openedAt !== null,
    refetchInterval: POLL_MS,
  })
  const conn = list.data?.find((c) => c.id === connectionId)
  const done =
    openedAt !== null &&
    conn?.stackName === ACCESS_STACK_NAME &&
    conn.lastValidatedAt !== null &&
    new Date(conn.lastValidatedAt).getTime() > openedAt
  useEffect(() => {
    if (done) onConnected()
  }, [done, onConnected])

  if (!setup.data) {
    return (
      <Card>
        {setup.error ? (
          <Alert tone="error">
            {setup.error instanceof ApiError ? setup.error.message : 'Could not load this connection.'}
          </Alert>
        ) : (
          <p className="text-sm text-secondary">Loading…</p>
        )}
      </Card>
    )
  }
  const s = setup.data
  const onSingleStack = s.connection.stackName === ACCESS_STACK_NAME
  return (
    <Card>
      <h1 className="text-2xl font-semibold">
        {onSingleStack ? 'Set up AWS access again' : 'Move to the single CloudOptics stack'}
      </h1>
      <p className="mt-1 text-sm text-secondary">
        {onSingleStack ? (
          <>
            First open your <code className="font-mono text-xs">{ACCESS_STACK_NAME}</code> stack in AWS and choose{' '}
            <strong>Delete</strong>. When it is gone, follow the steps below. Your costs and history stay.
          </>
        ) : (
          'One stack now covers your bill, recommendations and every member account, and future updates. Your costs and history stay. CloudOptics removes the old stacks for you.'
        )}
      </p>
      {onSingleStack && (
        <div className="mt-3">
          <a className="text-sm text-accent underline" href={s.stackConsoleUrl} target="_blank" rel="noreferrer">
            Open the stack in AWS
          </a>
        </div>
      )}
      <AwsSteps accountId={s.connection.awsAccountId} />
      <div className="mt-6 flex flex-col gap-4">
        {s.accessQuickCreateUrl ? (
          <a
            href={s.accessQuickCreateUrl}
            target="_blank"
            rel="noreferrer"
            className={LINK_STYLE}
            onClick={() => setOpenedAt((t) => t ?? Date.now())}
          >
            Open AWS and connect <ExternalLink size={16} />
          </a>
        ) : (
          <>
            <ByHand
              templateUrl={s.accessTemplateDownloadUrl}
              values={handValues({
                externalId: s.externalId,
                principalArn: s.principalArn,
                resourceTemplateUrl: s.resourceTemplateUrl,
                callbackUrl: s.callbackUrl,
                connectionRef: s.connectionRef,
                tenantId,
              })}
            />
            {openedAt === null && (
              <div className="flex justify-end">
                <Button onClick={() => setOpenedAt(Date.now())}>I have created the stack</Button>
              </div>
            )}
          </>
        )}
        {openedAt !== null && <Waiting since={openedAt} onRetry={() => setOpenedAt(Date.now())} error={null} />}
      </div>
      <div className="mt-6">
        <Link to="/settings/connections" className="text-sm text-secondary underline">
          Back to Connections
        </Link>
      </div>
    </Card>
  )
}

/** ``connectionId`` opens the AWS step for an existing connection (move or set up again). */
export function Onboarding({ startAt = 0, connectionId }: { startAt?: number; connectionId?: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const { data: me } = useMe()
  const [currentTenant] = useCurrentTenantId(me)
  const [step, setStep] = useState(connectionId ? 1 : startAt)
  const [tenantId, setTenantId] = useState<string | null>(startAt > 0 || connectionId ? currentTenant : null)
  const [checking, setChecking] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [country, setCountry] = useState('NG')
  const tid = tenantId ?? currentTenant

  const createWorkspace = (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setFieldErrors({})
    api
      .createTenant({ name, countryCode: country })
      .then(async (tenant) => {
        rememberTenant(tenant.id)
        setTenantId(tenant.id)
        await qc.invalidateQueries({ queryKey: ['me'] })
        setStep(1)
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          setError(err.message)
          setFieldErrors(Object.fromEntries(err.fieldErrors.map((f) => [f.field, f.message])))
        } else {
          setError('Something went wrong. Try again.')
        }
      })
      .finally(() => setBusy(false))
  }

  const connected = (id: string) => {
    void qc.invalidateQueries({ queryKey: ['connections'] })
    setChecking(id)
    setStep(2)
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
          <p className="mt-1 text-sm text-secondary">Your country sets the local currency and time zone.</p>
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

      {step === 1 && tid && !connectionId && <ConnectNew tenantId={tid} onConnected={connected} />}
      {step === 1 && tid && connectionId && (
        <ConnectExisting tenantId={tid} connectionId={connectionId} onConnected={() => connected(connectionId)} />
      )}
      {step === 2 && tid && checking && <CheckAccess tenantId={tid} connectionId={checking} />}
    </main>
  )
}
