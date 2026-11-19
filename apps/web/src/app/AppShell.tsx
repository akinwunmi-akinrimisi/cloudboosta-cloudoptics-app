import { Link, Outlet, useNavigate, useRouterState } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  ChartLine,
  FileText,
  Handshake,
  LayoutDashboard,
  Lightbulb,
  LogOut,
  Menu,
  PiggyBank,
  Plug,
  Settings,
} from 'lucide-react'

import { useEffect, useRef, useState } from 'react'

import type { CurrencyMode } from '../lib/currency'
import { MFA_REQUIRED_MESSAGE, isMfaRequired } from '../api/types'
import { Drawer } from '../components/overlay'
import { Alert, Button, Logo, cx } from '../components/ui'
import { DeletionBanner } from '../pages/DataDeletion'
import { SupportBanner } from '../pages/Staff'
import { useApi, useCurrentTenantId, useMe } from './context'
import { useRouteFocus } from './routeFocus'

const NAV = [
  { to: '/overview', label: 'Overview', icon: LayoutDashboard },
  { to: '/costs', label: 'Cost Explorer', icon: ChartLine },
  { to: '/recommendations', label: 'Recommendations', icon: Lightbulb },
  { to: '/anomalies', label: 'Anomalies', icon: Bell },
  { to: '/budgets', label: 'Budgets', icon: PiggyBank },
  { to: '/commitments', label: 'Commitments', icon: Handshake },
  { to: '/reports', label: 'Reports', icon: FileText },
  { to: '/settings/connections', label: 'Connections', icon: Plug },
  { to: '/settings', label: 'Settings', icon: Settings },
] as const

const MODES: [CurrencyMode, string][] = [
  ['usd', 'USD'],
  ['local', 'Local'],
  ['both', 'Both'],
]

function MainNav({ onNavigate }: { onNavigate?: () => void }) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-1 px-2 pb-4">
      {NAV.map(({ to, label, icon: Icon }) => (
        <Link
          key={to}
          to={to}
          activeOptions={{ exact: to === '/settings' }}
          className="flex min-h-10 items-center gap-3 rounded-sm px-3 py-2 text-sm text-sidebar-muted hover:text-white"
          activeProps={{ className: 'bg-sidebar-active !text-white' }}
          onClick={onNavigate}
        >
          <Icon size={18} aria-hidden="true" />
          {label}
        </Link>
      ))}
    </nav>
  )
}

export function AppShell() {
  const api = useApi()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { data: me } = useMe()
  const [tenantId, setTenantId] = useCurrentTenantId(me)
  const tenant = useQuery({
    queryKey: ['tenant', tenantId],
    queryFn: () => api.getTenant(tenantId as string),
    enabled: tenantId !== null,
  })
  const setView = useMutation({
    mutationFn: (view: CurrencyMode) => api.updateMe(view),
    onSuccess: (updated) => qc.setQueryData(['me'], updated),
  })
  const signOut = () => {
    api.signOut()
    qc.clear()
    void navigate({ to: '/sign-in' })
  }
  // Every workspace page would fail the same way, so explain once instead.
  const mfaRequired = isMfaRequired(tenant.error)
  const membership = me?.memberships.find((m) => m.tenantId === tenantId)
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  const mainRef = useRef<HTMLElement>(null)
  // The location once the new page has rendered, so its heading is the one found.
  const pathname = useRouterState({ select: (state) => (state.resolvedLocation ?? state.location).pathname })
  useRouteFocus(pathname, mainRef)
  useEffect(() => {
    // The drawer is for small screens only; widening the window closes it.
    const wide = window.matchMedia?.('(min-width: 768px)')
    const onChange = () => wide?.matches && setMenuOpen(false)
    wide?.addEventListener('change', onChange)
    return () => wide?.removeEventListener('change', onChange)
  }, [])

  return (
    <div className="min-h-screen md:grid md:grid-cols-[240px_minmax(0,1fr)] print:block">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded-sm focus:bg-surface-1 focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-primary focus:shadow-lg"
        onClick={(e) => {
          // Hash URLs route in the demo build, so move focus without changing the URL.
          e.preventDefault()
          mainRef.current?.focus()
        }}
      >
        Skip to main content
      </a>
      <aside className="hidden bg-brand-navy text-white md:block md:min-h-screen print:hidden" inert={menuOpen}>
        <div className="flex h-14 items-center gap-2 px-4 font-semibold">
          <Logo size={26} /> Cloudoptics
        </div>
        <MainNav />
      </aside>
      <Drawer open={menuOpen} onClose={() => setMenuOpen(false)} label="Cloudoptics" trigger={menuButton}>
        <MainNav onNavigate={() => setMenuOpen(false)} />
      </Drawer>
      <div className="min-w-0" inert={menuOpen}>
        <header className="flex h-14 items-center justify-between gap-2 border-b border-border bg-surface-1 px-2 sm:gap-3 sm:px-4 print:hidden">
          <div className="flex min-w-0 items-center gap-2">
            <button
              ref={menuButton}
              type="button"
              aria-label="Open menu"
              aria-haspopup="dialog"
              aria-expanded={menuOpen}
              className="inline-flex size-10 shrink-0 items-center justify-center rounded-sm text-primary hover:bg-surface-2 md:hidden"
              onClick={() => setMenuOpen(true)}
            >
              <Menu size={20} aria-hidden="true" />
            </button>
            {me && me.memberships.length > 1 ? (
              <select
                aria-label="Workspace"
                className="h-8 min-w-0 max-w-48 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
                value={tenantId ?? ''}
                onChange={(e) => setTenantId(e.target.value)}
              >
                {me.memberships.map((m) => (
                  <option key={m.tenantId} value={m.tenantId}>
                    {m.support ? `${m.tenantName} (support, read-only)` : m.tenantName}
                  </option>
                ))}
              </select>
            ) : (
              <span className="truncate text-sm font-medium">{tenant.data?.name ?? ''}</span>
            )}
            {me?.staff && (
              <Link to="/staff" className="text-xs font-medium text-secondary underline">
                Staff
              </Link>
            )}
            {api.mode === 'demo' && (
              <span className="rounded-full bg-brand-yellow px-2 py-0.5 text-xs font-semibold text-brand-navy">
                Demo
              </span>
            )}
          </div>
          <div className="flex shrink-0 items-center gap-1 sm:gap-2">
            <div role="group" aria-label="Currency" className="flex overflow-hidden rounded-sm border border-border-strong">
              {MODES.map(([mode, label]) => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={me?.preferredCurrencyView === mode}
                  className={cx(
                    'min-h-7 px-2 text-xs font-medium sm:px-2.5',
                    me?.preferredCurrencyView === mode ? 'bg-accent text-on-accent' : 'bg-surface-1 text-primary',
                  )}
                  onClick={() => setView.mutate(mode)}
                >
                  {label}
                </button>
              ))}
            </div>
            <button
              type="button"
              aria-label="Sign out"
              className="inline-flex size-10 items-center justify-center rounded-sm text-secondary hover:bg-surface-2"
              onClick={signOut}
            >
              <LogOut size={18} aria-hidden="true" />
            </button>
          </div>
        </header>
        <main
          id="main"
          ref={mainRef}
          tabIndex={-1}
          className="mx-auto max-w-6xl px-4 py-6 focus:outline-none md:px-6 print:max-w-none print:p-0"
        >
          {mfaRequired ? (
            <div className="space-y-3">
              <Alert tone="warning">{MFA_REQUIRED_MESSAGE}</Alert>
              <Button variant="secondary" onClick={signOut}>
                Sign out
              </Button>
            </div>
          ) : (
            <>
              {membership?.support && (
                <SupportBanner tenantName={membership.tenantName} support={membership.support} />
              )}
              {tenant.data && (
                <DeletionBanner
                  tenant={tenant.data}
                  isAdmin={me?.memberships.find((m) => m.tenantId === tenantId)?.role === 'admin'}
                />
              )}
              <Outlet />
            </>
          )}
        </main>
      </div>
    </div>
  )
}
