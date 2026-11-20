import { Link, useNavigate } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Building,
  Check,
  Clock,
  Cloud,
  Coins,
  Copy,
  Download,
  ExternalLink,
  FileText,
  Layers,
  Link2,
  LogIn,
  MousePointerClick,
  Pencil,
  Receipt,
  Search,
  Server,
  ShieldCheck,
  SquareCheck,
  Trash,
  Wallet,
} from 'lucide-react'
import type { FormEvent, ReactNode } from 'react'
import { useEffect, useState } from 'react'

import type { Connection, IntentSetup } from '../api/types'
import { ACCESS_STACK_NAME, ApiError } from '../api/types'
import { rememberTenant, useApi, useCurrentTenantId, useMe } from '../app/context'
import type { NextStep } from '../components/onboarding'
import { AccessPanel, Checks, CopyRow, Disclosure, Flow, Kicker, Lede, Next, Todo, TodoItem } from '../components/onboarding'
import { PermissionPanel, SiteLinks } from '../components/permissions'
import { Alert, Button, Field, Logo, StatusPill, cx } from '../components/ui'
import { connectionLabel, providerOf } from '../lib/providers'
import { DEMO_URL, HOW_IT_WORKS_STACK, absoluteSiteUrl } from '../lib/site'
import { GcpConnect } from './connect/GcpConnect'
import { CONNECT_PROVIDERS, DEFAULT_CONNECT } from './connect/providers'
import './onboarding.css'

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

// The same names and times as the public site (apps/site/src/data/claims.json, docs/14 §6.4);
// App.test.tsx fails when the two disagree.
export const STEPS = ['Workspace', 'Connect AWS', 'Check access']
export const AWS_MINUTES = 3
const POLL_MS = 5000
// After this long with no word from the stack, say so and offer the link again.
const SLOW_MS = 10 * 60 * 1000

function Progress({ step }: { step: number }) {
  return (
    <ol className="ed-progress" aria-label="Setup progress">
      {STEPS.map((label, i) => (
        <li key={label} className={cx(i < step && 'done')} aria-current={i === step ? 'step' : undefined}>
          <span className="pn" aria-hidden="true">
            {i < step ? <Check size={22} /> : i + 1}
          </span>
          <span>
            {label}
            {i < step && <span className="sr-only">, done</span>}
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

/** Creating the stack by hand: the alternative to the one-click link, and the only way when links are off. */
function ByHand({
  templateUrl,
  values,
  accountId,
  signIn,
}: {
  templateUrl: string
  values: { label: string; value: string }[]
  accountId?: string
  /** False under the one-click steps, which already say where to sign in. */
  signIn: boolean
}) {
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ol className="ed-steps">
        {signIn && (
          <li>
            In this browser, sign in to{' '}
            {accountId ? <>AWS account {accountId}</> : <>your main AWS account, the one that receives the bill</>}.
          </li>
        )}
        <li>Download the template.</li>
        <li>
          <a
            className="text-accent underline"
            href="https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create"
            target="_blank"
            rel="noreferrer"
          >
            CloudFormation → Create stack
          </a>{' '}
          → <strong>Upload a template file</strong> → name it <code className="text-sm">{ACCESS_STACK_NAME}</code>
        </li>
        <li>Paste the values below, tick the acknowledgement, Submit.</li>
      </ol>
      <div>
        <a href={templateUrl} download="cloudoptics-access.yaml" target="_blank" rel="noreferrer" className="ed-btn ghost">
          Download template <Download size={18} aria-hidden="true" />
        </a>
      </div>
      <div className="ed-kv">
        {values.map((v) => (
          <CopyRow key={v.label} label={v.label} value={v.value} />
        ))}
      </div>
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

/** What the person does in AWS: sign in, press one button, tick one box, come back. */
function AwsSteps({ accountId, button }: { accountId?: string; button: ReactNode }) {
  return (
    <Todo label="What to do in AWS">
      <TodoItem
        n={1}
        icon={LogIn}
        title={accountId ? `Sign in to AWS account ${accountId}` : 'Sign in to your billing AWS account'}
        hint="in this browser"
      />
      <TodoItem n={2} icon={MousePointerClick} title="Press the button" hint="AWS opens, filled in" go>
        {button}
      </TodoItem>
      <TodoItem n={3} icon={SquareCheck} title="Tick “I acknowledge”, then Create stack" hint="at the bottom" />
      <TodoItem n={4} icon={Clock} title="Come back here" time={`~${AWS_MINUTES} min`} />
    </Todo>
  )
}

/**
 * The one-click link is the way to connect. Deploying the template by hand is offered as an
 * alternative underneath (for teams that review templates first), and is the only way when
 * one-click links are off.
 */
function ConnectOptions({
  quickCreateUrl,
  templateUrl,
  values,
  accountId,
  opened,
  onOpened,
  colleagueMessage,
  waiting,
}: {
  quickCreateUrl: string | null
  templateUrl: string
  values: { label: string; value: string }[]
  accountId?: string
  opened: boolean
  onOpened: () => void
  /** Offers "Copy link for a colleague" with this text (docs/14 §6.2). */
  colleagueMessage?: string
  /** The "Waiting for AWS" line, shown under the steps once the link is used. */
  waiting?: ReactNode
}) {
  const byHand = (
    <>
      <ByHand templateUrl={templateUrl} values={values} accountId={accountId} signIn={!quickCreateUrl} />
      {!opened && (
        <div className="ed-actions">
          <button type="button" className="ed-btn" onClick={onOpened}>
            I have created the stack
          </button>
        </div>
      )}
    </>
  )
  if (!quickCreateUrl) {
    return (
      <div className="ed-sheet mt-7">
        {byHand}
        {waiting}
      </div>
    )
  }
  return (
    <>
      <AwsSteps
        accountId={accountId}
        button={
          <a href={quickCreateUrl} target="_blank" rel="noreferrer" className="ed-btn wide" onClick={onOpened}>
            Open AWS and connect <ExternalLink size={18} aria-hidden="true" />
            <span className="sr-only">(opens in a new tab)</span>
          </a>
        }
      />
      {colleagueMessage && <ForColleague message={colleagueMessage} onCopied={onOpened} />}
      {waiting}
      <Disclosure summary="Prefer to deploy the template yourself?">
        <div className="mt-3 flex flex-col gap-4">{byHand}</div>
      </Disclosure>
    </>
  )
}

/**
 * For someone without rights in the billing account: copies the one-click link with a short
 * note. The link carries only what AWS shows on its page anyway (the workspace's ExternalId and
 * ids), works for this workspace only and expires with the intent.
 */
function ForColleague({ message, onCopied }: { message: string; onCopied: () => void }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = () => {
    const done = () => {
      setState('copied')
      // The stack may now be created from another browser: start watching for it.
      onCopied()
    }
    if (!navigator.clipboard) return setState('failed')
    navigator.clipboard.writeText(message).then(done, () => setState('failed'))
  }
  return (
    <div className="mt-4 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
        <button type="button" className="ed-linkbtn" onClick={copy}>
          {state === 'copied' ? <Check size={18} aria-hidden="true" /> : <Link2 size={18} aria-hidden="true" />}
          Copy link for a colleague
        </button>
        <span role="status" className="ed-copied">
          {state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy. Copy the text below.' : ''}
        </span>
      </div>
      {state === 'failed' && (
        <textarea
          readOnly
          aria-label="Message for a colleague"
          rows={6}
          value={message}
          className="w-full rounded-sm border border-border-strong bg-surface-1 p-2 font-mono text-xs"
          onFocus={(e) => e.currentTarget.select()}
        />
      )}
    </div>
  )
}

export function colleagueMessage(quickCreateUrl: string, expiresAt?: string | null): string {
  const until = expiresAt
    ? ` It works until ${new Date(expiresAt).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}.`
    : ''
  return [
    `Could you connect our AWS account to Cloudoptics? It is read-only and takes about ${AWS_MINUTES} minutes.`,
    '',
    '1. Sign in to the AWS account that receives the bill (the management account if we use AWS Organizations).',
    '2. Open this link, tick the box that starts "I acknowledge" and choose Create stack:',
    quickCreateUrl,
    '',
    `What it creates: ${absoluteSiteUrl(HOW_IT_WORKS_STACK)}`,
    `The link is for our workspace only.${until}`,
  ].join('\n')
}

/** What the AWS stack lets Cloudoptics read, at a glance, with every permission one click away. */
function AwsAccess() {
  return (
    <AccessPanel
      title="Read-only access"
      reads={[
        { icon: Receipt, label: 'Bills and costs' },
        { icon: Wallet, label: 'Budgets, Savings Plans' },
        { icon: Server, label: 'Resources and metrics' },
      ]}
      never={[
        { icon: Pencil, label: 'Change anything' },
        { icon: FileText, label: 'Files, databases, secrets' },
      ]}
    >
      <PermissionPanel />
      <SiteLinks />
    </AccessPanel>
  )
}

/** The flow diagram for AWS: your account, the read-only stack, Cloudoptics. */
const AWS_FLOW = {
  from: { icon: Cloud, label: 'AWS account' },
  link: { icon: Layers, label: 'Read-only stack' },
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
          <button type="button" className="ed-btn ghost" onClick={onRetry}>
            Start again with a new link
          </button>
        </div>
      </div>
    )
  }
  const slow = now - since > SLOW_MS
  return (
    <div className="ed-wait" role="status">
      <span className="dot" aria-hidden="true" />
      <p className="font-semibold">Waiting for AWS…</p>
      <p>
        {slow
          ? 'Nothing from AWS yet. In CloudFormation, check that the cloudoptics-access stack exists and is not showing an error, or open the link again.'
          : 'AWS is creating read-only access for CloudOptics. Keep this page open.'}
      </p>
    </div>
  )
}

/** What happens after a healthy check, per cloud. */
function nextSteps(provider: string): NextStep[] {
  const checked: NextStep = { icon: ShieldCheck, label: 'Access checked', done: true }
  if (provider === 'gcp') {
    return [checked, { icon: Receipt, label: 'First costs', when: 'in minutes' }]
  }
  if (provider === 'digitalocean') {
    return [
      checked,
      { icon: Receipt, label: 'Invoices loaded', when: 'this month provisional', done: true },
      { icon: Search, label: 'Recommendations', when: 'after the first scan' },
    ]
  }
  return [
    checked,
    { icon: Search, label: 'Recommendations', when: 'in minutes' },
    { icon: Receipt, label: 'First costs', when: 'within a day' },
  ]
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
    <section className="max-w-[760px]">
      <h1 className="ed-h1">{running ? 'Checking access' : conn?.status === 'healthy' ? 'Connected' : 'Almost there'}</h1>
      {conn && (
        <p className="ed-sub">
          {providerOf(conn.provider) !== 'aws' ? (
            connectionLabel(conn)
          ) : (
            <>
              AWS account {conn.awsAccountId}
              {conn.accountType === 'management' ? ', with every account in your organization' : ''}
            </>
          )}
          .
        </p>
      )}
      {running && (
        <div className="ed-wait" role="status">
          <span className="dot" aria-hidden="true" />
          <p>Testing each permission…</p>
        </div>
      )}
      {error && (
        <div className="mt-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      {conn && !running && (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
            Connection status: <StatusPill status={conn.status} />
          </div>
          {conn.status === 'healthy' && <Next steps={nextSteps(providerOf(conn.provider))} />}
          <Checks checks={conn.permissionChecks} />
          <div className="ed-actions end">
            {conn.status !== 'healthy' && (
              <button type="button" className="ed-btn ghost" onClick={() => void again()} disabled={checking}>
                Check again
              </button>
            )}
            <button type="button" className="ed-btn" onClick={() => void navigate({ to: '/overview' })}>
              Go to my workspace
            </button>
          </div>
        </>
      )}
    </section>
  )
}

/** New account: one link, no form; the stack tells CloudOptics which account it is in. */
export function ConnectNew({ tenantId, onConnected }: { tenantId: string; onConnected: (connectionId: string) => void }) {
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
    <div className="ed-grid">
      <section className="min-w-0">
        <h1 className="ed-h1">Connect your AWS account</h1>
        <Flow label="How Cloudoptics connects to AWS" {...AWS_FLOW} />
        {error && (
          <div className="mt-4">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        {!setup && !error && (
          <p className="ed-sub mt-6" role="status">
            Preparing your link…
          </p>
        )}
        {setup && (
          <ConnectOptions
            quickCreateUrl={setup.quickCreateUrl}
            templateUrl={setup.templateDownloadUrl}
            values={handValues({ ...setup, tenantId })}
            opened={openedAt !== null}
            onOpened={() => setOpenedAt((t) => t ?? Date.now())}
            colleagueMessage={
              setup.quickCreateUrl ? colleagueMessage(setup.quickCreateUrl, setup.intent.expiresAt) : undefined
            }
            waiting={
              openedAt !== null && <Waiting since={openedAt} onRetry={start} error={intent.data?.error ?? null} />
            }
          />
        )}
      </section>
      <AwsAccess />
    </div>
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
      <div className="ed-sheet">
        {setup.error ? (
          <Alert tone="error">
            {setup.error instanceof ApiError ? setup.error.message : 'Could not load this connection.'}
          </Alert>
        ) : (
          <p className="ed-sub" role="status">
            Loading…
          </p>
        )}
      </div>
    )
  }
  const s = setup.data
  const onSingleStack = s.connection.stackName === ACCESS_STACK_NAME
  return (
    <div className="ed-grid">
      <section className="min-w-0">
        <Kicker>AWS account {s.connection.awsAccountId}</Kicker>
        <h1 className="ed-h1">{onSingleStack ? 'Set up AWS access again' : 'Move to the single CloudOptics stack'}</h1>
        {onSingleStack ? (
          <div className="ed-sheet mt-6">
            <p className="flex items-start gap-3">
              <Trash size={20} aria-hidden="true" className="mt-0.5 shrink-0" />
              <span>
                First open your <code className="text-sm">{ACCESS_STACK_NAME}</code> stack in AWS and choose{' '}
                <strong>Delete</strong>. When it is gone, follow the steps below. Your costs and history stay.
              </span>
            </p>
            <div className="mt-2 pl-8">
              <a className="text-accent underline" href={s.stackConsoleUrl} target="_blank" rel="noreferrer">
                Open the stack in AWS
              </a>
            </div>
          </div>
        ) : (
          <Lede icon={Layers}>One stack for bills, recommendations and every member account. History stays.</Lede>
        )}
        {!onSingleStack && (
          <p className="ed-sub">CloudOptics removes the old stacks for you, and future updates come through the new one.</p>
        )}
        <ConnectOptions
          quickCreateUrl={s.accessQuickCreateUrl}
          templateUrl={s.accessTemplateDownloadUrl}
          values={handValues({
            externalId: s.externalId,
            principalArn: s.principalArn,
            resourceTemplateUrl: s.resourceTemplateUrl,
            callbackUrl: s.callbackUrl,
            connectionRef: s.connectionRef,
            tenantId,
          })}
          accountId={s.connection.awsAccountId}
          opened={openedAt !== null}
          onOpened={() => setOpenedAt((t) => t ?? Date.now())}
          waiting={openedAt !== null && <Waiting since={openedAt} onRetry={() => setOpenedAt(Date.now())} error={null} />}
        />
        <div className="mt-6">
          <Link to="/settings/connections" className="inline-flex min-h-11 items-center text-secondary underline">
            Back to Connections
          </Link>
        </div>
      </section>
      <AwsAccess />
    </div>
  )
}

/** A new connection: "Which cloud?", then that provider's connect step (AWS by default). */
function ConnectStep({ tenantId, onConnected }: { tenantId: string; onConnected: (connectionId: string) => void }) {
  const [providerId, setProviderId] = useState<string>('aws')
  const Selected = (CONNECT_PROVIDERS.find((p) => p.id === providerId) ?? DEFAULT_CONNECT).Component
  return (
    <>
      <fieldset className="ed-picker">
        <legend className="ed-kicker">Which cloud?</legend>
        <div className="tiles">
          {CONNECT_PROVIDERS.map((p) => (
            <label key={p.id} className="ed-tile">
              <input
                type="radio"
                name="connect-provider"
                value={p.id}
                checked={p.id === providerId}
                onChange={() => setProviderId(p.id)}
              />
              <b className="min-w-0">{p.label}</b>
            </label>
          ))}
        </div>
      </fieldset>
      <Selected tenantId={tenantId} onConnected={onConnected} />
    </>
  )
}

/** Set up an existing connection again: Google Cloud shows its commands, AWS its stack. */
function ConnectAgain({
  tenantId,
  connectionId,
  onConnected,
}: {
  tenantId: string
  connectionId: string
  onConnected: () => void
}) {
  const api = useApi()
  const list = useQuery({ queryKey: ['connections', tenantId], queryFn: () => api.listConnections(tenantId) })
  const conn = list.data?.find((c) => c.id === connectionId)
  if (conn?.provider === 'gcp') {
    return <GcpConnect tenantId={tenantId} connectionId={connectionId} onConnected={onConnected} />
  }
  if (list.isPending) {
    return (
      <p className="ed-sub" role="status">
        Loading…
      </p>
    )
  }
  return <ConnectExisting tenantId={tenantId} connectionId={connectionId} onConnected={onConnected} />
}

// What a workspace's country sets, as the API does (apps/api identity/routes.py COUNTRY_DEFAULTS):
// shown beside the form, so the sentence that explained it is not needed.
const COUNTRY_DEFAULTS: Record<string, [currency: string, timeZone: string]> = {
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

/** A small statement slip: the workspace's name, the currencies its costs show in, its time zone. */
function WorkspaceSlip({ name, country }: { name: string; country: string }) {
  const local = COUNTRY_DEFAULTS[country]
  return (
    <div className="ed-slip" aria-label="Currency and time zone" role="group">
      {name.trim() && (
        <p className="mb-3 truncate font-semibold" aria-hidden="true">
          {name.trim()}
        </p>
      )}
      <dl>
        <dt>
          <Coins size={18} aria-hidden="true" />
          <span className="sr-only">Costs in</span>
        </dt>
        <dd>
          USD{local && <> · <mark>{local[0]}</mark></>}
        </dd>
        {local && (
          <>
            <dt>
              <Clock size={18} aria-hidden="true" />
              <span className="sr-only">Time zone</span>
            </dt>
            <dd>{local[1]}</dd>
          </>
        )}
      </dl>
    </div>
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
  // Already in a workspace: this is "New workspace" from the menu (the route guard sees to it).
  // Fixed on arrival, so creating the first workspace does not flip the heading.
  const [another] = useState(() => !!me?.memberships.length)
  const workspaceName = step > 0 ? me?.memberships.find((m) => m.tenantId === tid)?.tenantName : undefined

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
    <div className="ed">
      <header className="ed-top">
        <div>
          <span className="ed-brand">
            <Logo size={30} /> Cloudoptics
          </span>
          {workspaceName && <span className="ed-who">{workspaceName}</span>}
        </div>
      </header>
      <main className={cx('ed-main', step === 0 && 'narrow')}>
        <Progress step={step} />

        {step === 0 && (
          <section>
            <h1 className="ed-h1">{another ? 'Create another workspace' : 'Create your workspace'}</h1>
            {another && <Lede icon={Building}>One workspace per company, with its own members and clouds.</Lede>}
            <div className="ed-ws">
              <form className="ed-sheet ed-form" onSubmit={createWorkspace}>
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
                <button type="submit" className="ed-btn wide" disabled={busy}>
                  {busy ? 'Creating…' : 'Create workspace'}
                </button>
                {another && (
                  <Link to="/overview" className="inline-flex min-h-11 items-center self-start text-accent underline">
                    Back to {me?.memberships.find((m) => m.tenantId === currentTenant)?.tenantName ?? 'your workspace'}
                  </Link>
                )}
              </form>
              <WorkspaceSlip name={name} country={country} />
            </div>
            {api.mode !== 'demo' && (
              <p className="mt-8 flex flex-wrap items-center gap-x-2 text-secondary">
                Not ready?
                <a className="inline-flex min-h-11 items-center text-accent underline" href={DEMO_URL}>
                  Explore the sample workspace
                </a>
              </p>
            )}
          </section>
        )}

        {step === 1 && tid && !connectionId && <ConnectStep tenantId={tid} onConnected={connected} />}
        {step === 1 && tid && connectionId && (
          <ConnectAgain tenantId={tid} connectionId={connectionId} onConnected={() => connected(connectionId)} />
        )}
        {step === 2 && tid && checking && <CheckAccess tenantId={tid} connectionId={checking} />}
      </main>
    </div>
  )
}
