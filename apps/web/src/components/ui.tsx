import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react'
import { useId } from 'react'

import type { CheckStatus, ConnectionStatus } from '../api/types'

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(' ')
}

type Variant = 'primary' | 'secondary' | 'ghost'

export function Button({
  variant = 'primary',
  className,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={cx(
        'inline-flex h-9 items-center justify-center gap-2 rounded-sm px-4 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60',
        variant === 'primary' && 'bg-accent text-on-accent hover:bg-accent-hover',
        variant === 'secondary' && 'border border-border-strong bg-surface-1 text-primary hover:bg-surface-2',
        variant === 'ghost' && 'text-secondary hover:bg-surface-2',
        className,
      )}
      {...props}
    />
  )
}

export function Card({ children, className }: { children: ReactNode; className?: string }) {
  return <section className={cx('rounded-md border border-border bg-surface-1 p-6', className)}>{children}</section>
}

export function Field({
  label,
  hint,
  error,
  ...input
}: InputHTMLAttributes<HTMLInputElement> & { label: string; hint?: string; error?: string }) {
  const id = useId()
  const describedBy = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(' ') || undefined
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium text-primary">
        {label}
      </label>
      <input
        id={id}
        aria-invalid={error ? true : undefined}
        aria-describedby={describedBy}
        className={cx(
          'h-9 rounded-sm border bg-surface-1 px-3 text-sm text-primary placeholder:text-muted',
          error ? 'border-critical-ink' : 'border-border-strong',
        )}
        {...input}
      />
      {hint && (
        <p id={`${id}-hint`} className="text-xs text-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="text-xs text-critical-ink">
          {error}
        </p>
      )}
    </div>
  )
}

export function Alert({ tone, children }: { tone: 'info' | 'error' | 'warning'; children: ReactNode }) {
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={cx(
        'rounded-sm px-4 py-3 text-sm',
        tone === 'info' && 'bg-accent-soft text-primary',
        tone === 'error' && 'bg-critical-soft text-critical-ink',
        tone === 'warning' && 'bg-warning-soft text-warning-ink',
      )}
    >
      {children}
    </div>
  )
}

const STATUS_STYLE: Record<ConnectionStatus | CheckStatus, [string, string]> = {
  healthy: ['bg-good-soft text-good-ink', 'Healthy'],
  ok: ['bg-good-soft text-good-ink', 'OK'],
  degraded: ['bg-warning-soft text-warning-ink', 'Needs attention'],
  warning: ['bg-warning-soft text-warning-ink', 'Check'],
  failed: ['bg-critical-soft text-critical-ink', 'Failed'],
  error: ['bg-critical-soft text-critical-ink', 'Missing'],
  pending: ['bg-surface-2 text-secondary', 'Waiting for setup'],
  validating: ['bg-accent-soft text-primary', 'Checking…'],
  disconnected: ['bg-surface-2 text-secondary', 'Disconnected'],
  skipped: ['bg-surface-2 text-secondary', 'Skipped'],
}

export function StatusPill({ status }: { status: ConnectionStatus | CheckStatus }) {
  const [style, label] = STATUS_STYLE[status]
  return <span className={cx('inline-flex rounded-full px-2.5 py-0.5 text-xs font-medium', style)}>{label}</span>
}

export function Logo({ size = 28 }: { size?: number }) {
  return (
    <svg viewBox="0 0 64 64" width={size} height={size} aria-hidden="true">
      <rect width="64" height="64" rx="14" fill="#0B1F4B" />
      <circle cx="30" cy="34" r="17" fill="none" stroke="#FFFFFF" strokeWidth="5" />
      <path d="M19 34a11 11 0 0 1 11-11" fill="none" stroke="#5B8CFF" strokeWidth="5" strokeLinecap="round" />
      <circle cx="47" cy="17" r="6" fill="#FAC812" />
    </svg>
  )
}
