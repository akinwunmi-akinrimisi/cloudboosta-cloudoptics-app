import { useQuery } from '@tanstack/react-query'
import { Check, Copy, Database, KeyRound, Tag, Terminal } from 'lucide-react'
import type { FormEvent } from 'react'
import { useEffect, useRef, useState } from 'react'

import type { GcpConnectionCreate, GcpExportKind, GcpSetup } from '../../api/types'
import { ApiError } from '../../api/types'
import { useApi } from '../../app/context'
import { Disclosure, Flow, Lede } from '../../components/onboarding'
import { Alert, Field } from '../../components/ui'

export interface ConnectProps {
  tenantId: string
  onConnected: (connectionId: string) => void
}

// The API's patterns (POST /connections/gcp), checked here first so mistakes show at once.
const PATTERNS = {
  billingAccountId: /^[0-9A-F]{6}-[0-9A-F]{6}-[0-9A-F]{6}$/,
  exportProjectId: /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/,
  exportDatasetId: /^[A-Za-z0-9_]{1,1024}$/,
  exportTableId: /^[A-Za-z0-9_-]{1,1024}$/,
  queryProjectId: /^[a-z][a-z0-9-]{4,28}[a-z0-9]$/,
  datasetLocation: /^[A-Za-z0-9-]{2,40}$/,
}
type FieldName = keyof typeof PATTERNS

const PROJECT_RULE = '6 to 30 lowercase letters, digits or hyphens, starting with a letter.'
const MESSAGES: Record<FieldName, [missing: string, wrong: string]> = {
  billingAccountId: ['Enter the billing account ID.', 'Use the format 012345-6789AB-CDEF01.'],
  exportProjectId: ['Enter the project ID.', `A project ID has ${PROJECT_RULE}`],
  exportDatasetId: ['Enter the dataset name.', 'Use letters, digits and underscores only.'],
  exportTableId: ['Enter the table or view name.', 'Use letters, digits, underscores and hyphens only.'],
  queryProjectId: ['', `A project ID has ${PROJECT_RULE}`],
  datasetLocation: ['', 'Use a location such as US, EU or europe-west2.'],
}
const OPTIONAL: FieldName[] = ['queryProjectId', 'datasetLocation']

/** The table Google's managed FOCUS export writes: gcp_billing_export_focus_<billing account
 * ID>, dashes as underscores (its dataset is gcp_billing_immutable_<ID>_<location>). */
export function focusTableName(billingAccountId: string): string {
  const id = billingAccountId.trim().toUpperCase()
  return PATTERNS.billingAccountId.test(id) ? `gcp_billing_export_focus_${id.replace(/-/g, '_')}` : ''
}

/** Client-side checks; returns a message per field in error. */
export function validateGcp(body: GcpConnectionCreate): Partial<Record<FieldName, string>> {
  const errors: Partial<Record<FieldName, string>> = {}
  for (const name of Object.keys(PATTERNS) as FieldName[]) {
    const value = body[name] ?? ''
    if (!value) {
      if (!OPTIONAL.includes(name)) errors[name] = MESSAGES[name][0]
    } else if (!PATTERNS[name].test(value)) {
      errors[name] = MESSAGES[name][1]
    }
  }
  return errors
}

/** "billing_account_id" or "body.billingAccountId" from the API, as the form's field name. */
function fieldName(field: string): string {
  const last = field.split('.').pop() ?? field
  return last.replace(/_([a-z])/g, (_, c: string) => c.toUpperCase())
}

function useCopy(): [state: 'idle' | 'copied' | 'failed', copy: (text: string) => void] {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = (text: string) => {
    if (!navigator.clipboard) return setState('failed')
    navigator.clipboard.writeText(text).then(
      () => setState('copied'),
      () => setState('failed'),
    )
  }
  return [state, copy]
}

function CopyButton({ text, name, label = 'Copy' }: { text: string; name: string; label?: string }) {
  const [state, copy] = useCopy()
  return (
    <>
      <button type="button" className="ed-btn ghost sm" aria-label={`Copy ${name}`} onClick={() => copy(text)}>
        {state === 'copied' ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
        {state === 'copied' ? 'Copied' : label}
      </button>
      <span role="status" className="sr-only">
        {state === 'copied' ? `Copied ${name}` : state === 'failed' ? 'Could not copy. Select the text instead.' : ''}
      </span>
    </>
  )
}

const FLOW = {
  from: { icon: Database, label: 'BigQuery export' },
  link: { icon: KeyRound, label: 'Read-only access' },
}


/** Google Cloud: where the billing export is, then the commands that give read access to it. */
export function GcpConnect({ tenantId, onConnected, connectionId }: ConnectProps & { connectionId?: string }) {
  const api = useApi()
  const [created, setCreated] = useState<GcpSetup | null>(null)
  // "Set up again" for an existing connection: straight to the commands.
  const existing = useQuery({
    queryKey: ['gcp-setup', tenantId, connectionId],
    queryFn: () => api.getGcpSetup(tenantId, connectionId as string),
    enabled: Boolean(connectionId),
    retry: false,
  })

  if (connectionId) {
    if (existing.data) return <GrantAccess tenantId={tenantId} setup={existing.data} onConnected={onConnected} />
    return (
      <div className="ed-sheet">
        {existing.error ? (
          <Alert tone="error">
            {existing.error instanceof ApiError ? existing.error.message : 'Could not load this connection.'}
          </Alert>
        ) : (
          <p className="ed-sub" role="status">
            Loading…
          </p>
        )}
      </div>
    )
  }
  if (created) return <GrantAccess tenantId={tenantId} setup={created} onConnected={onConnected} focus />
  return <ExportForm tenantId={tenantId} onCreated={setCreated} />
}

function ExportForm({ tenantId, onCreated }: { tenantId: string; onCreated: (setup: GcpSetup) => void }) {
  const api = useApi()
  const [billingAccountId, setBillingAccountId] = useState('')
  const [exportProjectId, setExportProjectId] = useState('')
  const [exportDatasetId, setExportDatasetId] = useState('')
  const [exportTableId, setExportTableId] = useState('')
  const [exportKind, setExportKind] = useState<GcpExportKind>('focus_export')
  const [queryProjectId, setQueryProjectId] = useState('')
  const [datasetLocation, setDatasetLocation] = useState('')
  const [moreOpen, setMoreOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const body: GcpConnectionCreate = {
      billingAccountId: billingAccountId.trim().toUpperCase(),
      exportProjectId: exportProjectId.trim(),
      exportDatasetId: exportDatasetId.trim(),
      exportTableId: exportTableId.trim() || (exportKind === 'focus_export' ? focusTableName(billingAccountId) : ''),
      exportKind,
      queryProjectId: queryProjectId.trim() || null,
      datasetLocation: datasetLocation.trim() || null,
    }
    const errors = validateGcp(body)
    setFieldErrors(errors)
    if (Object.keys(errors).length) {
      setError('Check the fields marked below.')
      if (errors.queryProjectId || errors.datasetLocation) setMoreOpen(true)
      return
    }
    setBusy(true)
    setError(null)
    api
      .createGcpConnection(tenantId, body)
      .then(onCreated)
      .catch((err: unknown) => {
        if (err instanceof ApiError) {
          const byField = Object.fromEntries(err.fieldErrors.map((f) => [fieldName(f.field), f.message]))
          setError(err.message)
          setFieldErrors(byField)
          if (byField.queryProjectId || byField.datasetLocation) setMoreOpen(true)
        } else {
          setError('Something went wrong. Try again.')
        }
      })
      .finally(() => setBusy(false))
  }

  return (
    <div className="max-w-[760px]">
      <section className="min-w-0">
        <h1 className="ed-h1">Connect Google Cloud</h1>
        <Flow label="How Cloudoptics connects to Google Cloud" {...FLOW} />
        <form className="ed-sheet ed-form mt-7" onSubmit={submit} noValidate>
          <Field
            label="Billing account ID"
            hint="Billing, Account management"
            placeholder="012345-6789AB-CDEF01"
            required
            autoComplete="off"
            spellCheck={false}
            value={billingAccountId}
            onChange={(e) => setBillingAccountId(e.target.value.toUpperCase())}
            error={fieldErrors.billingAccountId}
          />
          <Field
            label="Project that holds the export"
            hint="Its project ID"
            placeholder="my-billing-project"
            required
            autoComplete="off"
            spellCheck={false}
            value={exportProjectId}
            onChange={(e) => setExportProjectId(e.target.value)}
            error={fieldErrors.exportProjectId}
          />
          <Field
            label="Dataset"
            hint="Managed export: gcp_billing_immutable_<ID>_<location>. View: its dataset."
            required
            autoComplete="off"
            spellCheck={false}
            value={exportDatasetId}
            onChange={(e) => setExportDatasetId(e.target.value)}
            error={fieldErrors.exportDatasetId}
          />
          <Field
            label="Table or view"
            hint={exportKind === 'focus_export' ? 'Empty: Google’s default table' : 'Your FOCUS view'}
            placeholder={exportKind === 'focus_export' ? focusTableName(billingAccountId) : undefined}
            required={exportKind !== 'focus_export'}
            autoComplete="off"
            spellCheck={false}
            value={exportTableId}
            onChange={(e) => setExportTableId(e.target.value)}
            error={fieldErrors.exportTableId}
          />
          <fieldset className="min-w-0">
            <legend className="mb-2 text-sm font-medium text-primary">Export type</legend>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {(
                [
                  ['focus_export', 'Managed FOCUS export (Preview)'],
                  ['focus_view', 'Detailed export + FOCUS view'],
                ] as const
              ).map(([value, label]) => (
                <label key={value} className="ed-tile">
                  <input
                    type="radio"
                    name="gcp-export-kind"
                    value={value}
                    checked={exportKind === value}
                    onChange={() => setExportKind(value)}
                  />
                  <span className="min-w-0 text-sm">{label}</span>
                </label>
              ))}
            </div>
            {fieldErrors.exportKind && <p className="mt-1 text-xs text-critical-ink">{fieldErrors.exportKind}</p>}
          </fieldset>
          <Disclosure summary="More options" className="mt-0" open={moreOpen} onToggle={setMoreOpen}>
            <div className="mt-4 flex flex-col gap-4">
              <Field
                label="Project that runs queries"
                hint="Empty: the export project. Billed for queries, under a cent a day."
                autoComplete="off"
                spellCheck={false}
                value={queryProjectId}
                onChange={(e) => setQueryProjectId(e.target.value)}
                error={fieldErrors.queryProjectId}
              />
              <Field
                label="Dataset location"
                hint="US, EU, europe-west2… Empty if unsure."
                autoComplete="off"
                spellCheck={false}
                value={datasetLocation}
                onChange={(e) => setDatasetLocation(e.target.value)}
                error={fieldErrors.datasetLocation}
              />
            </div>
          </Disclosure>
          {error && <Alert tone="error">{error}</Alert>}
          <button type="submit" disabled={busy} className="ed-btn self-end">
            {busy ? 'Saving…' : 'Continue'}
          </button>
        </form>
      </section>
    </div>
  )
}

function GrantAccess({
  tenantId,
  setup,
  onConnected,
  focus = false,
}: {
  tenantId: string
  setup: GcpSetup
  onConnected: (connectionId: string) => void
  /** Move focus to the heading (after the form, so screen readers hear the new step). */
  focus?: boolean
}) {
  const api = useApi()
  const heading = useRef<HTMLHeadingElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    if (focus) heading.current?.focus()
  }, [focus])
  const id = setup.connection.id
  const all = setup.commands.map((c) => `# ${c.title}\n${c.command}`).join('\n\n')

  const check = () => {
    setBusy(true)
    setError(null)
    api
      .validateConnection(tenantId, id)
      .then(() => onConnected(id))
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.')
        setBusy(false)
      })
  }

  return (
    <div className="max-w-[760px]">
      <section className="min-w-0">
        <h1 ref={heading} tabIndex={-1} className="ed-h1">
          Give Cloudoptics read access
        </h1>
        <Lede icon={Terminal}>Run in Cloud Shell or any signed-in terminal.</Lede>
        <p className="ed-sub flex items-start gap-2">
          <Tag size={16} aria-hidden="true" className="mt-0.5 shrink-0" />
          For a view, label every dataset it reads.
        </p>
        {!setup.enabled && (
          <div className="mt-4">
            <Alert tone="info">Google Cloud connections are not switched on for this Cloudoptics environment yet.</Alert>
          </div>
        )}
        <div className="ed-kv mt-6">
          <div>
            <span className="k">Cloudoptics service account</span>
            <code>{setup.serviceAccount}</code>
            <CopyButton text={setup.serviceAccount} name="service account" />
          </div>
        </div>
        <ol className="ed-todo" aria-label="Commands to run">
          {setup.commands.map((c, i) => (
            <li key={c.id}>
              <span className="n" aria-hidden="true">
                {i + 1}
              </span>
              <span className="ic" aria-hidden="true">
                <Terminal size={22} strokeWidth={1.75} />
              </span>
              <div className="body">
                <div className="ed-copyhead">
                  <h2 className="text-base font-semibold">
                    <span className="sr-only">{i + 1}. </span>
                    {c.title}
                    {c.optional && !/optional/i.test(c.title) && (
                      <span className="ml-2 text-xs font-normal text-secondary">Optional</span>
                    )}
                  </h2>
                  <CopyButton text={c.command} name={c.title} />
                </div>
                <pre className="ed-term">
                  <code>{c.command}</code>
                </pre>
              </div>
              <span className="tm" aria-hidden="true" />
            </li>
          ))}
        </ol>
        <div className="mt-4">
          <CopyButton text={all} name="all commands" label="Copy all" />
        </div>
        {setup.consoleSteps.length > 0 && (
          <Disclosure summary="Prefer the Google Cloud console?">
            <ol className="ed-steps">
              {setup.consoleSteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </Disclosure>
        )}
        {error && (
          <div className="mt-4">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        <div className="ed-actions end">
          <button type="button" className="ed-btn" onClick={check} disabled={busy || !setup.enabled}>
            {busy ? 'Checking…' : 'I have run the commands'}
          </button>
        </div>
      </section>
    </div>
  )
}
