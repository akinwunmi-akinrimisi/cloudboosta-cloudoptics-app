import { Link, Outlet, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  ChartLine,
  FileText,
  LayoutDashboard,
  Lightbulb,
  LogOut,
  PiggyBank,
  Plug,
  Settings,
} from 'lucide-react'

import type { CurrencyMode } from '../lib/currency'
import { Logo, cx } from '../components/ui'
import { useApi, useCurrentTenantId, useMe } from './context'

const NAV = [
  { to: '/overview', label: 'Overview', icon: LayoutDashboard },
  { to: '/costs', label: 'Cost Explorer', icon: ChartLine },
  { to: '/recommendations', label: 'Recommendations', icon: Lightbulb },
  { to: '/anomalies', label: 'Anomalies', icon: Bell },
  { to: '/budgets', label: 'Budgets', icon: PiggyBank },
  { to: '/reports', label: 'Reports', icon: FileText },
  { to: '/settings/connections', label: 'Connections', icon: Plug },
  { to: '/settings', label: 'Settings', icon: Settings },
] as const

const MODES: [CurrencyMode, string][] = [
  ['usd', 'USD'],
  ['local', 'Local'],
  ['both', 'Both'],
]

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

  return (
    <div className="min-h-screen md:grid md:grid-cols-[240px_1fr]">
      <aside className="bg-brand-navy text-white md:min-h-screen">
        <div className="flex h-14 items-center gap-2 px-4 font-semibold">
          <Logo size={26} /> Cloudoptics
        </div>
        <nav aria-label="Main" className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:pb-0">
          {NAV.map(({ to, label, icon: Icon }) => (
            <Link
              key={to}
              to={to}
              activeOptions={{ exact: to === '/settings' }}
              className="flex shrink-0 items-center gap-3 rounded-sm px-3 py-2 text-sm text-sidebar-muted hover:text-white"
              activeProps={{ className: 'bg-sidebar-active !text-white' }}
            >
              <Icon size={18} aria-hidden="true" />
              {label}
            </Link>
          ))}
        </nav>
      </aside>
      <div className="min-w-0">
        <header className="flex h-14 items-center justify-between gap-3 border-b border-border bg-surface-1 px-4">
          <div className="flex min-w-0 items-center gap-2">
            {me && me.memberships.length > 1 ? (
              <select
                aria-label="Workspace"
                className="h-8 rounded-sm border border-border-strong bg-surface-1 px-2 text-sm"
                value={tenantId ?? ''}
                onChange={(e) => setTenantId(e.target.value)}
              >
                {me.memberships.map((m) => (
                  <option key={m.tenantId} value={m.tenantId}>
                    {m.tenantName}
                  </option>
                ))}
              </select>
            ) : (
              <span className="truncate text-sm font-medium">{tenant.data?.name ?? ''}</span>
            )}
            {api.mode === 'demo' && (
              <span className="rounded-full bg-brand-yellow px-2 py-0.5 text-xs font-semibold text-brand-navy">
                Demo
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <div role="group" aria-label="Currency" className="flex overflow-hidden rounded-sm border border-border-strong">
              {MODES.map(([mode, label]) => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={me?.preferredCurrencyView === mode}
                  className={cx(
                    'px-2.5 py-1 text-xs font-medium',
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
              className="rounded-sm p-2 text-secondary hover:bg-surface-2"
              onClick={() => {
                api.signOut()
                qc.clear()
                void navigate({ to: '/sign-in' })
              }}
            >
              <LogOut size={18} />
            </button>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-4 py-6 md:px-6">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
