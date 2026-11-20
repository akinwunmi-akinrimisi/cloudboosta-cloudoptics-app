import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { CircleCheck, ShieldCheck } from 'lucide-react'
import { Suspense, lazy, useEffect, useMemo, useRef, useState } from 'react'

import { scopedApi } from '../api/handoff'
import type { Connection, ConnectInviteView, InviteSessionApi } from '../api/types'
import { ApiError } from '../api/types'
import { ApiProvider, useApi } from '../app/context'
import { InviteGuest } from '../components/handoff'
import { Checks, Kicker, Lede } from '../components/onboarding'
import { Alert, Logo, StatusPill } from '../components/ui'
import { INVITE_SESSION_KEY, INVITE_TENANT } from '../lib/handoff'
import { PROVIDER_LABEL, connectionLabel } from '../lib/providers'
import { ConnectNew, type SignInReturn } from './Onboarding'
import { DigitalOceanConnect, SIGN_IN_KEY } from './connect/DigitalOceanConnect'
import { GcpConnect } from './connect/GcpConnect'
import './onboarding.css'

const AzureConnect = lazy(() => import('./connect/AzureConnect'))

/** The token in the address after the link was opened: the session lives in this tab. */
export const OPENED = 'session'

/**
 * The page a cloud admin opens from a "send to my cloud admin" email (ADR-022): who asked,
 * that one cloud's connect step, its access check, then "Done". No account and no workspace:
 * the page runs on a connect-invite session that can do nothing else.
 */
export function ConnectInvitePage({ token, signIn }: { token: string; signIn?: SignInReturn }) {
  const api = useApi()
  const navigate = useNavigate()
  const [session, setSession] = useState<string | null>(() =>
    token === OPENED ? sessionStorage.getItem(INVITE_SESSION_KEY) : null,
  )
  const [error, setError] = useState<string | null>(null)
  const opening = useRef(false)
  useEffect(() => {
    // The link works for one session at a time: open it once (strict mode renders twice).
    if (token === OPENED || opening.current) return
    opening.current = true
    api
      .openConnectInvite(token)
      .then((r) => {
        sessionStorage.setItem(INVITE_SESSION_KEY, r.sessionToken)
        setSession(r.sessionToken)
        // Off the address bar: the link's token is not kept in history or sent on.
        void navigate({ to: '/connect-invite/$token', params: { token: OPENED }, replace: true })
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : 'Could not open this link. Try again.'))
  }, [api, navigate, token])

  const invite = useMemo(() => (session ? api.inviteSession(session) : null), [api, session])
  const guestApi = useMemo(() => (invite ? scopedApi(invite, api) : null), [api, invite])

  return (
    <div className="ed">
      <header className="ed-top">
        <div>
          <span className="ed-brand">
            <Logo size={30} /> Cloudoptics
          </span>
        </div>
      </header>
      <main className="ed-main">
        {error || (!session && token === OPENED) ? (
          <section className="max-w-[760px]">
            <h1 className="ed-h1">This link does not work</h1>
            <div className="mt-4">
              <Alert tone="error">{error ?? 'Open the link from your email again.'}</Alert>
            </div>
          </section>
        ) : !invite || !guestApi ? (
          <p className="ed-sub" role="status">
            Opening…
          </p>
        ) : (
          <ApiProvider api={guestApi}>
            <InviteGuest>
              <Invited invite={invite} signIn={signIn} />
            </InviteGuest>
          </ApiProvider>
        )}
      </main>
    </div>
  )
}

function Invited({ invite, signIn }: { invite: InviteSessionApi; signIn?: SignInReturn }) {
  const qc = useQueryClient()
  const [checking, setChecking] = useState<string | null>(null)
  // A connection already there when the page opened (a reload): its check, not the form.
  const [resumed, setResumed] = useState<boolean | null>(null)
  const view = useQuery({
    queryKey: ['connect-invite-view'],
    queryFn: async () => {
      const v = await invite.view()
      setResumed((r) => r ?? Boolean(v.connection))
      return v
    },
    // Followed until connected: the stack's callback, a check run elsewhere, the sender revoking.
    refetchInterval: (q) => (q.state.data?.status === 'connected' ? false : 4000),
    retry: false,
  })
  const v = view.data
  if (view.error) {
    return (
      <section className="max-w-[760px]">
        <h1 className="ed-h1">This link does not work</h1>
        <div className="mt-4">
          <Alert tone="error">{view.error instanceof ApiError ? view.error.message : 'Try the link from your email again.'}</Alert>
        </div>
      </section>
    )
  }
  if (!v) {
    return (
      <p className="ed-sub" role="status">
        Opening…
      </p>
    )
  }
  if (v.status === 'connected') return <Done view={v} />
  const connected = (id: string) => {
    setChecking(id)
    void qc.invalidateQueries({ queryKey: ['connect-invite-view'] })
  }
  return (
    <>
      <Asked view={v} />
      {signIn ? (
        <FinishSignIn invite={invite} back={signIn} onDone={connected} />
      ) : v.connection && (checking || (resumed && v.provider !== 'gcp')) ? (
        <InviteCheck invite={invite} connection={v.connection} />
      ) : (
        <Step view={v} onConnected={connected} />
      )}
    </>
  )
}

/** Who asked, for which company: one line, then what the step grants. */
function Asked({ view }: { view: ConnectInviteView }) {
  const who = view.requestedBy.name || view.requestedBy.email || 'Someone'
  return (
    <div className="mb-6 max-w-[760px]">
      <Kicker>For {view.workspaceName}</Kicker>
      <Lede icon={ShieldCheck}>
        {who} asked you to connect {PROVIDER_LABEL[view.provider]}. Read-only: {view.grants}. You do not join the workspace
        or see its costs.
      </Lede>
      <p className="ed-sub">{view.whoCan}.</p>
    </div>
  )
}

function Step({ view, onConnected }: { view: ConnectInviteView; onConnected: (id: string) => void }) {
  if (view.provider === 'gcp') {
    return <GcpConnect tenantId={INVITE_TENANT} onConnected={onConnected} connectionId={view.connection?.id} />
  }
  if (view.provider === 'digitalocean') return <DigitalOceanConnect tenantId={INVITE_TENANT} onConnected={onConnected} />
  if (view.provider === 'azure') {
    return (
      <Suspense
        fallback={
          <p role="status" className="text-sm text-secondary">
            Loading…
          </p>
        }
      >
        <AzureConnect tenantId={INVITE_TENANT} />
      </Suspense>
    )
  }
  return <ConnectNew tenantId={INVITE_TENANT} onConnected={onConnected} />
}

/** The connection this link set up: its access check, run again until it works. */
function InviteCheck({ invite, connection }: { invite: InviteSessionApi; connection: Connection }) {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const running = busy || connection.status === 'pending' || connection.status === 'validating'
  const again = async () => {
    setBusy(true)
    setError(null)
    try {
      await invite.check()
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.')
    } finally {
      setBusy(false)
      await qc.invalidateQueries({ queryKey: ['connect-invite-view'] })
    }
  }
  return (
    <section className="max-w-[760px]">
      <h1 className="ed-h1">{running ? 'Checking access' : 'Almost there'}</h1>
      <p className="ed-sub">{connectionLabel(connection)}.</p>
      {running ? (
        <div className="ed-wait" role="status">
          <span className="dot" aria-hidden="true" />
          <p>Testing each permission…</p>
        </div>
      ) : (
        <div className="mt-4 flex flex-wrap items-center gap-2 text-sm">
          Connection status: <StatusPill status={connection.status} />
        </div>
      )}
      {error && (
        <div className="mt-4">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      <Checks checks={connection.permissionChecks} />
      <div className="ed-actions">
        <button type="button" className="ed-btn" onClick={() => void again()} disabled={busy}>
          Check access
        </button>
      </div>
    </section>
  )
}

function Done({ view }: { view: ConnectInviteView }) {
  const who = view.requestedBy.name || view.requestedBy.email || 'The person who asked'
  return (
    <section className="max-w-[760px]">
      <Kicker>For {view.workspaceName}</Kicker>
      <h1 className="ed-h1">Done, you can close this page</h1>
      <Lede icon={CircleCheck}>
        {PROVIDER_LABEL[view.provider]} is connected, read-only. {who} has been told.
      </Lede>
      {view.connection && <Checks checks={view.connection.permissionChecks} />}
    </section>
  )
}

/** DigitalOcean sent the browser back to its callback page during an invite session. */
function FinishSignIn({
  invite,
  back,
  onDone,
}: {
  invite: InviteSessionApi
  back: SignInReturn
  onDone: (id: string) => void
}) {
  const navigate = useNavigate()
  const sent = useRef(false)
  const [error, setError] = useState<string | null>(
    back.error ? (back.error === 'access_denied' ? 'Not approved in DigitalOcean. Nothing was connected.' : 'DigitalOcean did not finish the sign-in.') : null,
  )
  useEffect(() => {
    if (sent.current || back.error || !back.code || !back.state) return
    sent.current = true
    invite
      .finishDigitalOceanOAuth(back.code, back.state)
      .then((conn) => {
        sessionStorage.removeItem(SIGN_IN_KEY)
        onDone(conn.id)
        void navigate({ to: '/connect-invite/$token', params: { token: OPENED }, replace: true })
      })
      .catch((err: unknown) => setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.'))
  }, [back, invite, navigate, onDone])
  if (!error) {
    return (
      <div className="ed-wait" role="status">
        <span className="dot" aria-hidden="true" />
        <p>Checking access…</p>
      </div>
    )
  }
  return (
    <>
      <div className="mb-6 max-w-[760px]">
        <Alert tone="error">{error}</Alert>
      </div>
      <DigitalOceanConnect tenantId={INVITE_TENANT} onConnected={onDone} />
    </>
  )
}
