import { useNavigate } from '@tanstack/react-router'
import { BadgeCheck, Check, Copy, Database, FileText, KeyRound, Receipt, ShieldCheck, Tag, Terminal } from 'lucide-react'
import type { FormEvent, TextareaHTMLAttributes } from 'react'
import { useEffect, useId, useRef, useState } from 'react'

import type { AzureConnection, AzureConnectionCreate, AzureConnectionSetup, AzureScopeType } from '../../api/types'
import { ApiError } from '../../api/types'
import { useApi } from '../../app/context'
import { Checks, Chips, Disclosure, Flow, NewTab, Next, Todo, TodoItem } from '../../components/onboarding'
import { Alert, Button, Field, StatusPill, cx } from '../../components/ui'
import { accountLabel } from '../../lib/providers'

export interface WizardProps {
  tenantId: string
  /** Called once the connection exists. */
  onCreated?: () => void
}

// Connect Azure: a form for the directory, the scope and the FOCUS export's storage, then the
// three things to do in Azure (consent, read roles, the cost export) and the access check.

const GUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const STORAGE_ACCOUNT = /^[a-z0-9]{3,24}$/
const CONTAINER = /^[a-z0-9](?!.*--)[a-z0-9-]{1,61}[a-z0-9]$/
const MAX_SUBSCRIPTIONS = 50
const GUID_EXAMPLE = '00000000-0000-0000-0000-000000000000'

export interface AzureFormValues {
  directoryId: string
  scopeType: AzureScopeType
  subscriptionIds: string
  billingAccountId: string
  storageAccount: string
  container: string
  exportPath: string
}

type FormErrors = Partial<Record<keyof AzureFormValues, string>>

/** Splits a comma- or newline-separated list, drops blanks and repeats. */
export function splitIds(text: string): string[] {
  return [...new Set(text.split(/[\s,;]+/).map((s) => s.trim().toLowerCase()).filter(Boolean))]
}

/** Checks the form; returns the request body when everything is valid. */
export function validateAzureForm(v: AzureFormValues): { errors: FormErrors; body: AzureConnectionCreate | null } {
  const errors: FormErrors = {}
  const directoryId = v.directoryId.trim().toLowerCase()
  if (!directoryId) errors.directoryId = 'Enter the directory (tenant) ID.'
  else if (!GUID.test(directoryId)) errors.directoryId = `Enter the directory ID as a GUID, like ${GUID_EXAMPLE}.`

  const subscriptionIds = splitIds(v.subscriptionIds)
  const badId = subscriptionIds.find((id) => !GUID.test(id))
  if (v.scopeType === 'subscription' && subscriptionIds.length === 0)
    errors.subscriptionIds = 'Enter the subscription ID.'
  else if (badId) errors.subscriptionIds = `“${badId}” is not a subscription ID. Use the GUID, like ${GUID_EXAMPLE}.`
  else if (v.scopeType === 'subscription' && subscriptionIds.length > 1)
    errors.subscriptionIds =
      'Connect one subscription at a time (each has its own cost export), or connect the billing account.'
  else if (subscriptionIds.length > MAX_SUBSCRIPTIONS)
    errors.subscriptionIds = `List up to ${MAX_SUBSCRIPTIONS} subscriptions.`

  const billingAccountId = v.billingAccountId.trim()
  if (v.scopeType === 'billing_account') {
    if (!billingAccountId) errors.billingAccountId = 'Enter the billing account ID.'
    else if (/[\s/]/.test(billingAccountId)) errors.billingAccountId = 'Enter the billing account ID only, without spaces or slashes.'
  }

  const storageAccount = v.storageAccount.trim()
  if (!STORAGE_ACCOUNT.test(storageAccount))
    errors.storageAccount = 'Use 3 to 24 lowercase letters and numbers, with no hyphens.'

  const container = v.container.trim()
  if (!CONTAINER.test(container))
    errors.container =
      'Use 3 to 63 lowercase letters, numbers and single hyphens, starting and ending with a letter or number.'

  const exportPath = v.exportPath.trim().replace(/^\/+|\/+$/g, '')
  if (exportPath.includes('\\')) errors.exportPath = 'Use forward slashes (/) between folders.'

  if (Object.keys(errors).length > 0) return { errors, body: null }
  return {
    errors,
    body: {
      directoryId,
      scopeType: v.scopeType,
      subscriptionIds,
      billingAccountId: v.scopeType === 'billing_account' ? billingAccountId : null,
      storageAccount,
      container,
      exportPath,
    },
  }
}

/** "subscription_ids[0]" or "body.subscriptionIds" → "subscriptionIds". */
function fieldKey(field: string): string {
  const last = field.replace(/\[\d+\]$/, '').split('.').pop() ?? field
  return last.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())
}

function TextArea({
  label,
  hint,
  error,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & { label: string; hint?: string; error?: string }) {
  const id = useId()
  const describedBy = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(' ') || undefined
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium text-primary">
        {label}
      </label>
      <textarea
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cx(
          'rounded-sm border bg-surface-1 px-3 py-2 font-mono text-sm text-primary placeholder:text-muted',
          error ? 'border-critical-ink' : 'border-border-strong',
        )}
        {...props}
      />
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-xs break-words text-critical-ink">
          {error}
        </p>
      )}
    </div>
  )
}

const EMPTY: AzureFormValues = {
  directoryId: '',
  scopeType: 'subscription',
  subscriptionIds: '',
  billingAccountId: '',
  storageAccount: '',
  container: '',
  exportPath: '',
}

const SCOPES: { id: AzureScopeType; label: string; hint: string }[] = [
  { id: 'subscription', label: 'Subscription', hint: 'One subscription' },
  { id: 'billing_account', label: 'Billing account', hint: 'All it bills' },
]

function AzureForm({ tenantId, onCreated }: { tenantId: string; onCreated: (s: AzureConnectionSetup) => void }) {
  const api = useApi()
  const [values, setValues] = useState<AzureFormValues>(EMPTY)
  const [errors, setErrors] = useState<FormErrors>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Bumped after a refused submit, to move focus to the first field with an error.
  const [refused, setRefused] = useState(0)
  const form = useRef<HTMLFormElement>(null)
  useEffect(() => {
    if (refused) form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus()
  }, [refused])

  const set = (key: keyof AzureFormValues) => (value: string) => {
    setValues((v) => ({ ...v, [key]: value }))
    setErrors((e) => ({ ...e, [key]: undefined }))
  }

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    const checked = validateAzureForm(values)
    setErrors(checked.errors)
    if (!checked.body) {
      setRefused((n) => n + 1)
      return
    }
    setBusy(true)
    api
      .createAzureConnection(tenantId, checked.body)
      .then(onCreated)
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          setError(err.message)
          const fields = Object.fromEntries(err.fieldErrors.map((f) => [fieldKey(f.field), f.message])) as FormErrors
          if (Object.keys(fields).length) {
            setErrors(fields)
            setRefused((n) => n + 1)
          }
        } else {
          setError('Something went wrong. Try again.')
        }
      })
      .finally(() => setBusy(false))
  }

  const billing = values.scopeType === 'billing_account'
  return (
    <div className="max-w-[760px]">
      <section className="min-w-0">
        <h1 className="ed-h1">Connect your Azure subscription</h1>
        <Flow label="How Cloudoptics connects to Azure" {...FLOW} />
        <form ref={form} className="ed-sheet ed-form mt-7" onSubmit={submit} noValidate>
          <Field
            label="Directory (tenant) ID"
            hint="Entra ID, Overview, Tenant ID"
            placeholder={GUID_EXAMPLE}
            autoComplete="off"
            spellCheck={false}
            value={values.directoryId}
            onChange={(e) => set('directoryId')(e.target.value)}
            error={errors.directoryId}
          />
          <fieldset className="min-w-0">
            <legend className="mb-2 text-sm font-medium text-primary">Connect</legend>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {SCOPES.map((s) => (
                <label key={s.id} className="ed-tile">
                  <input
                    type="radio"
                    name="azure-scope"
                    value={s.id}
                    checked={values.scopeType === s.id}
                    onChange={() => set('scopeType')(s.id)}
                    aria-labelledby={`azure-scope-${s.id}-label`}
                    aria-describedby={`azure-scope-${s.id}`}
                  />
                  <span className="min-w-0">
                    <b id={`azure-scope-${s.id}-label`}>{s.label}</b>
                    <small id={`azure-scope-${s.id}`}>{s.hint}</small>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          {billing && (
            <Field
              label="Billing account ID"
              hint="Cost Management, Billing scopes, Properties"
              autoComplete="off"
              spellCheck={false}
              value={values.billingAccountId}
              onChange={(e) => set('billingAccountId')(e.target.value)}
              error={errors.billingAccountId}
            />
          )}
          {billing ? (
            <TextArea
              label="Subscription IDs to scan (optional)"
              hint={`For recommendations. Commas or new lines, up to ${MAX_SUBSCRIPTIONS}.`}
              rows={3}
              placeholder={GUID_EXAMPLE}
              autoComplete="off"
              spellCheck={false}
              value={values.subscriptionIds}
              onChange={(e) => set('subscriptionIds')(e.target.value)}
              error={errors.subscriptionIds}
            />
          ) : (
            <Field
              label="Subscription ID"
              hint="Subscriptions, Subscription ID column"
              placeholder={GUID_EXAMPLE}
              autoComplete="off"
              spellCheck={false}
              value={values.subscriptionIds}
              onChange={(e) => set('subscriptionIds')(e.target.value)}
              error={errors.subscriptionIds}
            />
          )}
          <Field
            label="Storage account name"
            hint="Holds the cost export"
            autoComplete="off"
            spellCheck={false}
            value={values.storageAccount}
            onChange={(e) => set('storageAccount')(e.target.value)}
            error={errors.storageAccount}
          />
          <Field
            label="Container"
            autoComplete="off"
            spellCheck={false}
            value={values.container}
            onChange={(e) => set('container')(e.target.value)}
            error={errors.container}
          />
          <Field
            label="Export folder path (optional)"
            hint="Empty for the top"
            autoComplete="off"
            spellCheck={false}
            value={values.exportPath}
            onChange={(e) => set('exportPath')(e.target.value)}
            error={errors.exportPath}
          />
          {error && <Alert tone="error">{error}</Alert>}
          <button type="submit" disabled={busy} className="ed-btn wide">
            {busy ? 'Saving…' : 'Continue'}
          </button>
        </form>
      </section>
    </div>
  )
}

const FLOW = {
  from: { icon: Database, label: 'FOCUS export' },
  link: { icon: KeyRound, label: 'Read-only roles' },
}

const ROLES = ['Reader', 'Cost Management Reader', 'Storage Blob Data Reader']

/** A value to paste somewhere else, wrapped (never scrolled sideways), with a Copy button. */
export function CopyBlock({
  label,
  value,
  paper = false,
}: {
  label: string
  value: string
  /** The onboarding's paper look (pages/onboarding.css). Off for the Connections page, which
   * does not load that stylesheet. */
  paper?: boolean
}) {
  const id = useId()
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = () => {
    if (!navigator.clipboard) return setState('failed')
    navigator.clipboard.writeText(value).then(
      () => setState('copied'),
      () => setState('failed'),
    )
  }
  const status = state === 'copied' ? 'Copied' : state === 'failed' ? 'Could not copy. Select the text and copy it.' : ''
  const icon = state === 'copied' ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />
  if (paper) {
    return (
      <div className="flex min-w-0 flex-col gap-1">
        <div className="ed-copyhead">
          <span id={id}>{label}</span>
          <span className="flex flex-wrap items-center gap-2">
            <span role="status" className="ed-copied">
              {status}
            </span>
            <button type="button" className="ed-btn ghost sm" onClick={copy} aria-label={`Copy ${label}`}>
              {icon}
              Copy
            </button>
          </span>
        </div>
        <pre aria-labelledby={id} tabIndex={0} className="ed-term scroll">
          {value}
        </pre>
      </div>
    )
  }
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span id={id} className="text-xs font-medium text-secondary">
          {label}
        </span>
        <span className="flex items-center gap-2">
          <span role="status" className="text-xs text-secondary">
            {status}
          </span>
          <Button variant="secondary" onClick={copy} aria-label={`Copy ${label}`}>
            {icon}
            Copy
          </Button>
        </span>
      </div>
      <pre
        aria-labelledby={id}
        tabIndex={0}
        className="max-h-72 overflow-y-auto rounded-sm bg-surface-2 px-3 py-2 font-mono text-xs break-all whitespace-pre-wrap"
      >
        {value}
      </pre>
    </div>
  )
}

function Steps({ items }: { items: string[] }) {
  return (
    <ol className="ed-steps">
      {items.map((s) => (
        <li key={s}>{s}</li>
      ))}
    </ol>
  )
}

/** "Azure subscription <id>" for each subscription, or the billing account. */
export function azureScopeLabel(c: Pick<AzureConnection, 'scopeType' | 'subscriptionIds' | 'billingAccountId'>): string {
  if (c.scopeType === 'billing_account' && c.billingAccountId) return `Azure billing account ${c.billingAccountId}`
  return c.subscriptionIds.map((id) => accountLabel('azure', id)).join(', ')
}

function AzureSetupView({ tenantId, setup }: { tenantId: string; setup: AzureConnectionSetup }) {
  const api = useApi()
  const navigate = useNavigate()
  const [conn, setConn] = useState<AzureConnection>(setup.connection)
  const [checked, setChecked] = useState(setup.connection.lastValidatedAt !== null)
  const [checking, setChecking] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  // The form is gone: put focus on the new heading so keyboard and screen-reader users follow.
  useEffect(() => heading.current?.focus(), [])

  const check = async () => {
    setChecking(true)
    setError(null)
    try {
      setConn(await api.validateAzureConnection(tenantId, conn.id))
      setChecked(true)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.')
    } finally {
      setChecking(false)
    }
  }

  const usable = checked && (conn.status === 'healthy' || conn.status === 'degraded')
  const title = checking
    ? 'Checking access'
    : !checked
      ? 'Set up access in Azure'
      : conn.status === 'healthy'
        ? 'Connected'
        : 'Almost there'
  const announce = checking
    ? 'Checking access…'
    : checked
      ? conn.status === 'healthy'
        ? 'Access checked. Connected.'
        : 'Access checked. Some checks need attention.'
      : ''

  return (
    <section className="max-w-[760px]">
      <h1 ref={heading} tabIndex={-1} className="ed-h1">
        {title}
      </h1>
      <p className="ed-sub break-all">{azureScopeLabel(conn)}</p>
      {conn.status !== 'healthy' && (
        <Todo label="Three steps in Azure, then check access here">
          <TodoItem
            n={1}
            icon={BadgeCheck}
            title="Grant admin consent"
            hint={
              <>
                Global or Privileged Role Administrator of <code className="break-all">{conn.directoryId}</code>
              </>
            }
          >
            <div>
              <NewTab href={setup.consentUrl} className="ed-btn">
                Grant admin consent
              </NewTab>
            </div>
          </TodoItem>
          <TodoItem
            n={2}
            icon={Terminal}
            title="Give Cloudoptics read access"
            hint="Owner or User Access Administrator, Cloud Shell (Bash)"
          >
            <Chips items={ROLES} label="Read-only roles it adds" />
            <p className="flex items-start gap-2 text-sm text-secondary">
              <Tag size={16} aria-hidden="true" className="mt-0.5 shrink-0" />
              Also tags your storage account, as proof it is yours.
            </p>
            <CopyBlock paper label="Azure CLI command" value={setup.roleAssignmentCommand} />
            <Disclosure summary="Prefer the Azure portal?" className="mt-0">
              <Steps items={setup.portalRoleSteps} />
              <div className="mt-3 flex flex-col gap-3">
                <CopyBlock paper label="Cloudoptics application (client) ID" value={setup.clientId} />
                <CopyBlock paper label={`Tag ${setup.ownershipTag}`} value={setup.ownershipCode} />
              </div>
            </Disclosure>
          </TodoItem>
          <TodoItem n={3} icon={FileText} title="Create a daily FOCUS cost export">
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-sm">
              <dt className="text-secondary">Storage account</dt>
              <dd className="wrap-anywhere">
                <code>{conn.storageAccount}</code>
              </dd>
              <dt className="text-secondary">Container</dt>
              <dd className="wrap-anywhere">
                <code>{conn.container}</code>
              </dd>
              <dt className="text-secondary">Folder</dt>
              <dd className="wrap-anywhere">{conn.exportPath ? <code>{conn.exportPath}</code> : 'The top'}</dd>
            </dl>
            <div>
              <NewTab href={setup.exportPortalUrl} className="ed-btn">
                Open cost exports in Azure
              </NewTab>
            </div>
            <Disclosure summary="How to create the export" className="mt-0">
              <Steps items={setup.exportSteps} />
            </Disclosure>
          </TodoItem>
        </Todo>
      )}

      <p role="status" className="mt-6 font-semibold">
        {announce}
      </p>
      {error && (
        <div className="mt-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      {checked && !checking && (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
            Connection status: <StatusPill status={conn.status} />
          </div>
          {usable && (
            <Next
              steps={[
                { icon: ShieldCheck, label: 'Access checked', done: true },
                { icon: Receipt, label: 'First Azure costs', when: 'within a day' },
              ]}
            />
          )}
          <Checks checks={conn.permissionChecks} />
        </>
      )}
      <div className="ed-actions end">
        {!checked ? (
          <button type="button" className="ed-btn" onClick={() => void check()} disabled={checking}>
            Check access
          </button>
        ) : (
          conn.status !== 'healthy' && (
            <button
              type="button"
              className={cx('ed-btn', usable && 'ghost')}
              onClick={() => void check()}
              disabled={checking}
            >
              Check again
            </button>
          )
        )}
        {usable && (
          <button type="button" className="ed-btn" onClick={() => void navigate({ to: '/overview' })}>
            Go to my workspace
          </button>
        )}
      </div>
    </section>
  )
}

/** The Connect Azure wizard: the form, then the setup in Azure and the access check. */
export default function AzureConnect({ tenantId, onCreated }: WizardProps) {
  const [setup, setSetup] = useState<AzureConnectionSetup | null>(null)
  if (!setup) {
    return (
      <AzureForm
        tenantId={tenantId}
        onCreated={(s) => {
          setSetup(s)
          onCreated?.()
        }}
      />
    )
  }
  return <AzureSetupView tenantId={tenantId} setup={setup} />
}
