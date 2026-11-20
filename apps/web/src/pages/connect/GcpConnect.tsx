import { useQuery } from '@tanstack/react-query'
import { Check, Copy } from 'lucide-react'
import type { FormEvent } from 'react'
import { useEffect, useRef, useState } from 'react'

import type { GcpConnectionCreate, GcpExportKind, GcpSetup } from '../../api/types'
import { ApiError } from '../../api/types'
import { useApi } from '../../app/context'
import { Alert, Button, Card, Field } from '../../components/ui'

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
      <Button variant="secondary" aria-label={`Copy ${name}`} onClick={() => copy(text)}>
        {state === 'copied' ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
        {state === 'copied' ? 'Copied' : label}
      </Button>
      <span role="status" className="sr-only">
        {state === 'copied' ? `Copied ${name}` : state === 'failed' ? 'Could not copy. Select the text instead.' : ''}
      </span>
    </>
  )
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
      <Card>
        {existing.error ? (
          <Alert tone="error">
            {existing.error instanceof ApiError ? existing.error.message : 'Could not load this connection.'}
          </Alert>
        ) : (
          <p className="text-sm text-secondary">Loading…</p>
        )}
      </Card>
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
  const [exportTableId, setExportTableId] = useState('focus')
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
      exportTableId: exportTableId.trim(),
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
    <Card>
      <h1 className="text-2xl font-semibold">Connect Google Cloud</h1>
      <p className="mt-1 text-sm text-secondary">
        Cloudoptics reads your billing export in BigQuery, read-only. It never changes anything.
      </p>
      <form className="mt-6 flex flex-col gap-4" onSubmit={submit} noValidate>
        <Field
          label="Billing account ID"
          hint="In Billing, Account management. Looks like 012345-6789AB-CDEF01."
          required
          autoComplete="off"
          spellCheck={false}
          value={billingAccountId}
          onChange={(e) => setBillingAccountId(e.target.value.toUpperCase())}
          error={fieldErrors.billingAccountId}
        />
        <Field
          label="Project that holds the export"
          hint="The project ID, such as my-billing-project."
          required
          autoComplete="off"
          spellCheck={false}
          value={exportProjectId}
          onChange={(e) => setExportProjectId(e.target.value)}
          error={fieldErrors.exportProjectId}
        />
        <Field
          label="Dataset"
          hint="The BigQuery dataset the billing export writes to."
          required
          autoComplete="off"
          spellCheck={false}
          value={exportDatasetId}
          onChange={(e) => setExportDatasetId(e.target.value)}
          error={fieldErrors.exportDatasetId}
        />
        <Field
          label="Table or view"
          hint="The FOCUS export table, or the FOCUS view you made."
          required
          autoComplete="off"
          spellCheck={false}
          value={exportTableId}
          onChange={(e) => setExportTableId(e.target.value)}
          error={fieldErrors.exportTableId}
        />
        <fieldset className="flex flex-col gap-2">
          <legend className="mb-1 text-sm font-medium text-primary">Export type</legend>
          {(
            [
              ['focus_export', 'Managed FOCUS export (Preview)'],
              ['focus_view', 'Detailed export with the FOCUS view'],
            ] as const
          ).map(([value, label]) => (
            <label key={value} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name="gcp-export-kind"
                value={value}
                checked={exportKind === value}
                onChange={() => setExportKind(value)}
              />
              {label}
            </label>
          ))}
          {fieldErrors.exportKind && <p className="text-xs text-critical-ink">{fieldErrors.exportKind}</p>}
        </fieldset>
        <details
          className="rounded-sm border border-border px-3 py-2"
          open={moreOpen}
          onToggle={(e) => setMoreOpen(e.currentTarget.open)}
        >
          <summary className="cursor-pointer text-sm font-medium">More options</summary>
          <div className="mt-3 flex flex-col gap-4">
            <Field
              label="Project that runs queries"
              hint="Leave empty to use the export project. Queries are billed to this project, usually well under a cent a day."
              autoComplete="off"
              spellCheck={false}
              value={queryProjectId}
              onChange={(e) => setQueryProjectId(e.target.value)}
              error={fieldErrors.queryProjectId}
            />
            <Field
              label="Dataset location"
              hint="Such as US, EU or europe-west2. Leave empty if you are not sure."
              autoComplete="off"
              spellCheck={false}
              value={datasetLocation}
              onChange={(e) => setDatasetLocation(e.target.value)}
              error={fieldErrors.datasetLocation}
            />
          </div>
        </details>
        {error && <Alert tone="error">{error}</Alert>}
        <Button type="submit" disabled={busy} className="self-end">
          {busy ? 'Saving…' : 'Continue'}
        </Button>
      </form>
    </Card>
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
    <Card>
      <h1 ref={heading} tabIndex={-1} className="text-2xl font-semibold">
        Give Cloudoptics read access
      </h1>
      <p className="mt-1 text-sm text-secondary">
        Run these commands in Cloud Shell, or in a terminal signed in to Google Cloud. The first adds a label that proves
        the dataset is yours.
      </p>
      {!setup.enabled && (
        <div className="mt-4">
          <Alert tone="info">Google Cloud connections are not switched on for this Cloudoptics environment yet.</Alert>
        </div>
      )}
      <div className="mt-4 flex flex-col gap-1">
        <span className="text-xs font-medium text-secondary">Cloudoptics service account</span>
        <div className="flex items-center gap-2">
          <code className="min-w-0 flex-1 truncate rounded-sm bg-surface-2 px-3 py-2 font-mono text-xs">
            {setup.serviceAccount}
          </code>
          <CopyButton text={setup.serviceAccount} name="service account" />
        </div>
      </div>
      <ol className="mt-6 flex flex-col gap-4">
        {setup.commands.map((c, i) => (
          <li key={c.id} className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-medium">
                {i + 1}. {c.title}
                {c.optional && !/optional/i.test(c.title) && (
                  <span className="ml-2 text-xs font-normal text-muted">Optional</span>
                )}
              </h2>
              <CopyButton text={c.command} name={c.title} />
            </div>
            <pre className="whitespace-pre-wrap break-all rounded-sm bg-surface-2 px-3 py-2 font-mono text-xs">
              <code>{c.command}</code>
            </pre>
          </li>
        ))}
      </ol>
      <div className="mt-4">
        <CopyButton text={all} name="all commands" label="Copy all" />
      </div>
      {setup.consoleSteps.length > 0 && (
        <details className="mt-6 rounded-sm border border-border px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">Prefer the Google Cloud console?</summary>
          <ol className="mt-2 list-decimal space-y-1 pl-5 text-sm text-secondary">
            {setup.consoleSteps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </details>
      )}
      {error && (
        <div className="mt-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      <div className="mt-6 flex justify-end">
        <Button onClick={check} disabled={busy || !setup.enabled}>
          {busy ? 'Checking…' : 'I have run the commands'}
        </Button>
      </div>
    </Card>
  )
}
