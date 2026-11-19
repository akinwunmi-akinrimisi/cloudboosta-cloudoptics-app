import { Link } from '@tanstack/react-router'
import type { ReactNode } from 'react'

import type { Role } from '../api/types'

// The Settings sub-nav (docs/08 §1.7): Connections has its own page; the other sections
// are parts of /settings, and a section link scrolls to its part.

export type SettingsSection = 'users' | 'notifications' | 'preferences' | 'security' | 'workspace'

export const SECTIONS: { id: SettingsSection; label: string; roles: Role[] }[] = [
  { id: 'users', label: 'Users & roles', roles: ['admin', 'member', 'viewer'] },
  { id: 'notifications', label: 'Notifications', roles: ['admin', 'member', 'viewer'] },
  { id: 'preferences', label: 'Preferences', roles: ['admin', 'member', 'viewer'] },
  { id: 'security', label: 'Security', roles: ['admin'] },
  { id: 'workspace', label: 'Workspace', roles: ['admin'] },
]

export function isSection(value: unknown): value is SettingsSection {
  return SECTIONS.some((s) => s.id === value)
}

const itemClass = 'block shrink-0 rounded-sm px-3 py-1.5 text-sm text-secondary hover:bg-surface-2 hover:text-primary'
const activeClass = 'bg-surface-2 font-medium !text-primary'

export function SettingsLayout({
  current,
  role,
  children,
}: {
  current: SettingsSection | 'connections' | null
  role: Role | undefined
  children: ReactNode
}) {
  return (
    <div className="flex flex-col gap-6 md:grid md:grid-cols-[180px_1fr] md:items-start">
      <nav aria-label="Settings" className="flex gap-1 overflow-x-auto md:sticky md:top-4 md:flex-col">
        <Link to="/settings/connections" className={`${itemClass} ${current === 'connections' ? activeClass : ''}`}>
          AWS connections
        </Link>
        {SECTIONS.filter((s) => role !== undefined && s.roles.includes(role)).map((s) => (
          <Link
            key={s.id}
            to="/settings"
            search={{ section: s.id }}
            className={`${itemClass} ${current === s.id ? activeClass : ''}`}
          >
            {s.label}
          </Link>
        ))}
      </nav>
      <div className="flex min-w-0 flex-col gap-6">{children}</div>
    </div>
  )
}
