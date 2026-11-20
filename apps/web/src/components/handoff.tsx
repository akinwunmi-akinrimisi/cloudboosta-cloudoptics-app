import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Send, UserRound } from 'lucide-react'
import type { FormEvent, ReactNode } from 'react'
import { createContext, useContext, useEffect, useRef, useState } from 'react'

import type { ConnectInvite, ConnectInviteStatus, Provider } from '../api/types'
import { ApiError } from '../api/types'
import { useApi } from '../app/context'
import { INVITE_STATUS_LABEL, WHO_CAN } from '../lib/handoff'
import { PROVIDER_LABEL } from '../lib/providers'
import { useEscapeToClose } from './overlay'
import { Alert, Field, cx } from './ui'

// "Send this to my cloud admin" (ADR-022): the role pre-check line before each cloud's
// connect step, the hand-off form, and the status of what was sent.

const GuestContext = createContext(false)

/** Inside a connect-invite session the connect steps leave out what is for workspace members. */
export function InviteGuest({ children }: { children: ReactNode }) {
  return <GuestContext.Provider value>{children}</GuestContext.Provider>
}

export function useInviteGuest(): boolean {
  return useContext(GuestContext)
}

const CHIP: Record<ConnectInviteStatus, string> = {
  sent: 'bg-accent-soft text-primary',
  opened: 'bg-warning-soft text-warning-ink',
  connected: 'bg-good-soft text-good-ink',
  expired: 'bg-surface-2 text-secondary',
  revoked: 'bg-surface-2 text-secondary',
}

export function InviteChip({ status }: { status: ConnectInviteStatus }) {
  return (
    <span className={cx('inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium', CHIP[status])}>
      {INVITE_STATUS_LABEL[status]}
    </span>
  )
}

function message(err: unknown): string {
  return err instanceof ApiError ? err.message : 'Something went wrong. Try again.'
}

/** Polls a workspace's requests while one is still waiting for its admin. */
export function useConnectInvites(tenantId: string, enabled = true) {
  const api = useApi()
  return useQuery({
    queryKey: ['connect-invites', tenantId],
    queryFn: () => api.listConnectInvites(tenantId),
    enabled,
    refetchInterval: (q) => (q.state.data?.some((i) => i.status === 'sent' || i.status === 'opened') ? 5000 : false),
  })
}

function SentLine({ invite }: { invite: ConnectInvite }) {
  return (
    <span className="flex flex-wrap items-center gap-2">
      <span className="min-w-0 break-all">{invite.email}</span>
      <InviteChip status={invite.status} />
    </span>
  )
}

/**
 * One line on who can connect this cloud, and "Not you? Send this to your admin": their
 * email, Send, then the request's status here (sent, opened, connected, expired).
 */
export function RoleCheck({
  tenantId,
  provider,
  onConnected,
}: {
  tenantId: string
  provider: Provider
  /** Their admin connected it: the connect step can move on. */
  onConnected?: (connectionId: string) => void
}) {
  const api = useApi()
  const qc = useQueryClient()
  const guest = useInviteGuest()
  const [open, setOpen] = useState(false)
  const [email, setEmail] = useState('')
  const [sentId, setSentId] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLFormElement>(null)
  useEscapeToClose(open, () => setOpen(false), trigger, panel)
  const send = useMutation({
    mutationFn: () => api.sendConnectInvite(tenantId, email.trim(), provider),
    onSuccess: (invite) => {
      setSentId(invite.id)
      setOpen(false)
      setEmail('')
      void qc.invalidateQueries({ queryKey: ['connect-invites', tenantId] })
    },
  })
  const list = useConnectInvites(tenantId, !guest && sentId !== null)
  const sent = list.data?.find((i) => i.id === sentId) ?? send.data
  const connectedId = sent?.status === 'connected' ? sent.connectionId : null
  const told = useRef(false)
  useEffect(() => {
    if (!connectedId || !onConnected || told.current) return
    told.current = true
    onConnected(connectedId)
  }, [connectedId, onConnected])
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (email.trim()) send.mutate()
  }
  const fieldError = send.error instanceof ApiError ? send.error.fieldErrors.find((f) => f.field.endsWith('email')) : undefined

  return (
    <div className="mb-6 flex max-w-[760px] flex-col gap-3 text-sm" data-testid="role-check">
      <p className="flex items-start gap-2 text-secondary">
        <UserRound size={18} aria-hidden="true" className="mt-0.5 shrink-0" />
        <span>{WHO_CAN[provider]}</span>
      </p>
      {!guest && !open && (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 pl-7">
          <button
            ref={trigger}
            type="button"
            className="ed-linkbtn"
            aria-expanded={false}
            onClick={() => {
              send.reset()
              setOpen(true)
            }}
          >
            <Send size={18} aria-hidden="true" />
            {sent ? 'Send to someone else' : 'Not you? Send this to your admin'}
          </button>
          {sent && (
            <span role="status" className="flex flex-wrap items-center gap-2">
              <span>Sent to</span>
              <SentLine invite={sent} />
            </span>
          )}
        </div>
      )}
      {!guest && sent && !open && !sent.emailSent && sent.demoLink && (
        <p className="pl-7 text-secondary">
          Demo: nothing is emailed.{' '}
          <a className="text-accent underline" href={sent.demoLink}>
            Open the link your admin gets
          </a>
        </p>
      )}
      {!guest && sent && !open && !sent.emailSent && !sent.demoLink && (
        <div className="pl-7">
          <Alert tone="warning">The email could not be sent. Try again in a few minutes.</Alert>
        </div>
      )}
      {!guest && open && (
        <form ref={panel} className="ed-sheet ed-form" onSubmit={submit} aria-label={`Send the ${PROVIDER_LABEL[provider]} step`}>
          <Field
            label="Your admin's email"
            type="email"
            required
            autoComplete="off"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            hint={`They get a link to this ${PROVIDER_LABEL[provider]} step only: no costs, no workspace.`}
            error={fieldError?.message}
          />
          {send.error && !fieldError && <Alert tone="error">{message(send.error)}</Alert>}
          <div className="flex flex-wrap gap-3">
            <button type="submit" className="ed-btn" disabled={send.isPending || !email.trim()}>
              {send.isPending ? 'Sending…' : 'Send'}
            </button>
            <button type="button" className="ed-btn ghost" onClick={() => setOpen(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

/** The Connections page: what was sent to cloud admins, with status; Withdraw while open. */
export function ConnectInvites({ tenantId }: { tenantId: string }) {
  const api = useApi()
  const qc = useQueryClient()
  const list = useConnectInvites(tenantId)
  const revoke = useMutation({
    mutationFn: (id: string) => api.revokeConnectInvite(tenantId, id),
    onSettled: () => qc.invalidateQueries({ queryKey: ['connect-invites', tenantId] }),
  })
  const [weekAgo] = useState(() => Date.now() - 7 * 86_400_000)
  const invites = (list.data ?? []).filter((i) => i.status !== 'revoked')
  if (!invites.length) return null
  const news = invites.filter((i) => i.status === 'connected' && i.connectedAt && new Date(i.connectedAt).getTime() > weekAgo)
  return (
    <section aria-labelledby="sent-to-admins" className="flex flex-col gap-3">
      <h2 id="sent-to-admins" className="text-lg font-semibold">
        Sent to cloud admins
      </h2>
      {news.map((i) => (
        <Alert key={i.id} tone="info">
          {i.email} connected {PROVIDER_LABEL[i.provider]}.
        </Alert>
      ))}
      {revoke.error && <Alert tone="error">{message(revoke.error)}</Alert>}
      <ul className="divide-y divide-border rounded-sm border border-border bg-surface-1">
        {invites.map((i) => (
          <li key={i.id} className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 text-sm">
            <div className="flex min-w-0 flex-col gap-1">
              <span className="font-medium">{PROVIDER_LABEL[i.provider]}</span>
              <SentLine invite={i} />
            </div>
            {(i.status === 'sent' || i.status === 'opened') && (
              <button
                type="button"
                className="inline-flex min-h-11 items-center text-secondary underline"
                onClick={() => revoke.mutate(i.id)}
                disabled={revoke.isPending}
              >
                Withdraw<span className="sr-only"> the request to {i.email}</span>
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
