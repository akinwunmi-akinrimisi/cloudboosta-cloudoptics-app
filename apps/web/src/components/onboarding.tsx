import { ArrowRight, Check, ChevronDown, CircleCheck, CircleDashed, CircleExclamationPoint, CircleX, Copy, ExternalLink } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type { CSSProperties, ReactNode } from 'react'
import { useId, useState } from 'react'

import type { PermissionCheck } from '../api/types'
import { StatusPill, cx } from './ui'

// Shared parts of the onboarding pages in design Direction A (pages/onboarding.css): a
// headline block, the "how it connects" diagram, the numbered to-do ledger, the read-only
// panel, the access-check ledger and the "what happens next" timeline. Graphics are inline SVG
// (lucide icons) or CSS; decorative ones are aria-hidden.

/** The small mono line above a headline: "Step 2 of 3". */
export function Kicker({ children }: { children: ReactNode }) {
  return <span className="ed-kicker">{children}</span>
}

/** One short promise beside an icon, under the headline. */
export function Lede({ icon: Icon, children }: { icon: LucideIcon; children: ReactNode }) {
  return (
    <p className="ed-lede">
      <span>
        <Icon size={18} aria-hidden="true" />
        {children}
      </span>
    </p>
  )
}

export interface FlowNode {
  icon: LucideIcon
  label: string
  detail: string
}

/**
 * Where the data is, the read-only link and Cloudoptics, as three boxes with arrows: replaces
 * the paragraph that used to explain it. The boxes are a list, so screen readers read the
 * three steps in order; the arrows are decoration.
 */
export function Flow({ from, link, label }: { from: FlowNode; link: FlowNode; label: string }) {
  const nodes: [FlowNode, string][] = [
    [from, 'node'],
    [link, 'node link'],
    [{ icon: CircleCheck, label: 'Cloudoptics', detail: 'reads, never writes' }, 'node us'],
  ]
  return (
    <ol className="ed-flow" aria-label={label}>
      {nodes.map(([n, cls], i) => (
        <FlowItem key={n.label} node={n} className={cls} arrow={i > 0} />
      ))}
    </ol>
  )
}

function FlowItem({ node, className, arrow }: { node: FlowNode; className: string; arrow: boolean }) {
  const Icon = node.icon
  return (
    <>
      {arrow && (
        <li className="arrow" aria-hidden="true">
          <ArrowRight size={24} strokeWidth={1.75} />
        </li>
      )}
      <li className={className}>
        <Icon size={22} strokeWidth={1.75} aria-hidden="true" />
        <b>{node.label}</b>
        <span>{node.detail}</span>
      </li>
    </>
  )
}

/** The numbered steps: a ruled ledger with an icon per step. */
export function Todo({ label, children }: { label: string; children: ReactNode }) {
  return (
    <ol className="ed-todo" aria-label={label}>
      {children}
    </ol>
  )
}

export function TodoItem({
  n,
  icon: Icon,
  title,
  hint,
  time,
  go = false,
  children,
}: {
  n: number
  icon: LucideIcon
  title: ReactNode
  hint?: ReactNode
  time?: string
  /** The step with the main button: drawn as a raised sheet. */
  go?: boolean
  children?: ReactNode
}) {
  return (
    <li className={cx(go && 'go')}>
      <span className="n" aria-hidden="true">
        {n}
      </span>
      <span className="ic" aria-hidden="true">
        <Icon size={22} strokeWidth={1.75} />
      </span>
      <div className="body">
        <b>{title}</b>
        {hint && <span className="hint">{hint}</span>}
        {children && <div className="more">{children}</div>}
      </div>
      {time ? <span className="tm">{time}</span> : <span className="tm" aria-hidden="true" />}
    </li>
  )
}

export function Chips({ items, label }: { items: string[]; label: string }) {
  return (
    <ul className="ed-chips" aria-label={label}>
      {items.map((s) => (
        <li key={s}>{s}</li>
      ))}
    </ul>
  )
}

/** A collapsed section with a chevron (native details, so it works without script). */
export function Disclosure({
  summary,
  children,
  className,
  open,
  onToggle,
}: {
  summary: ReactNode
  children: ReactNode
  className?: string
  open?: boolean
  onToggle?: (open: boolean) => void
}) {
  return (
    <details
      className={cx('ed-details', className)}
      open={open}
      onToggle={onToggle ? (e) => onToggle(e.currentTarget.open) : undefined}
    >
      <summary>
        {summary}
        <ChevronDown size={20} className="chev" aria-hidden="true" />
      </summary>
      <div className="inner">{children}</div>
    </details>
  )
}

export function NewTab({ href, children, className }: { href: string; children: ReactNode; className?: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className={className}>
      {children} <ExternalLink size={16} aria-hidden="true" />
      <span className="sr-only">(opens in a new tab)</span>
    </a>
  )
}

type CopyState = 'idle' | 'copied' | 'failed'

export function useCopy(): [CopyState, (text: string) => void] {
  const [state, setState] = useState<CopyState>('idle')
  const copy = (text: string) => {
    if (!navigator.clipboard) return setState('failed')
    navigator.clipboard.writeText(text).then(
      () => setState('copied'),
      () => setState('failed'),
    )
  }
  return [state, copy]
}

/** A value to paste somewhere else (template parameter): name, value, Copy. The button's
 * parent holds the value's <code>, which the end-to-end tests read. */
export function CopyRow({ label, value }: { label: string; value: string }) {
  const [state, copy] = useCopy()
  return (
    <div>
      <span className="k">{label}</span>
      <code>{value}</code>
      <button type="button" className="ed-linkbtn" aria-label={`Copy ${label}`} onClick={() => copy(value)}>
        {state === 'copied' ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
        {state === 'copied' ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}

export interface AccessLine {
  icon: LucideIcon
  label: string
}

/** "What Cloudoptics can read": what it reads, what it never does, then details and links. */
export function AccessPanel({
  title,
  sub,
  reads,
  never,
  children,
}: {
  title: string
  sub?: string
  reads: AccessLine[]
  never: AccessLine[]
  children?: ReactNode
}) {
  const id = useId()
  return (
    // A section, not an aside: a complementary landmark inside <main> is an axe best-practice failure.
    <section className="ed-aside" aria-labelledby={`${id}-h`}>
      <div className="head">
        <h2 id={`${id}-h`} className="ed-h2">
          {title}
        </h2>
        {sub && <p className="ed-sub">{sub}</p>}
      </div>
      <p id={`${id}-r`} className="lh">
        Reads
      </p>
      <ul className="yes" aria-labelledby={`${id}-r`}>
        {reads.map(({ icon: Icon, label }) => (
          <li key={label} className="row">
            <Icon size={20} strokeWidth={1.75} aria-hidden="true" />
            {label}
          </li>
        ))}
      </ul>
      <p id={`${id}-n`} className="lh never">
        Never
      </p>
      <ul className="no" aria-labelledby={`${id}-n`}>
        {never.map(({ icon: Icon, label }) => (
          <li key={label} className="row">
            <Icon size={20} strokeWidth={1.75} aria-hidden="true" />
            {label}
          </li>
        ))}
      </ul>
      {children}
    </section>
  )
}

const MARK: Record<string, [LucideIcon, string]> = {
  ok: [CircleCheck, 'ok'],
  warning: [CircleExclamationPoint, 'warn'],
  error: [CircleX, 'bad'],
  skipped: [CircleDashed, 'skip'],
}

/** The access check, one ruled line per permission, with its result. */
export function Checks({ checks }: { checks: PermissionCheck[] }) {
  return (
    <ul className="ed-checks" aria-label="Access checks">
      {checks.map((c) => {
        const [Icon, tone] = MARK[c.status] ?? [CircleExclamationPoint, 'warn']
        return (
          <li key={c.id}>
            <Icon size={20} className={`mk ${tone}`} aria-hidden="true" />
            <div className="txt">
              <b>{c.label}</b>
              <p>{c.message}</p>
              {c.hint && <p>{c.hint}</p>}
            </div>
            <span className="pill">
              <StatusPill status={c.status} />
            </span>
          </li>
        )
      })}
    </ul>
  )
}

export interface NextStep {
  icon: LucideIcon
  label: string
  when: string
  done?: boolean
}

/** What happens after the access check, as a dashed timeline. */
export function Next({ steps }: { steps: NextStep[] }) {
  return (
    <ol className="ed-next" aria-label="What happens next" style={{ '--cols': steps.length } as CSSProperties}>
      {steps.map(({ icon: Icon, label, when, done }) => (
        <li key={label} className={cx(done && 'done')}>
          <span className="dot" aria-hidden="true">
            <Icon size={20} strokeWidth={1.75} />
          </span>
          <b>
            {label}
            {done && <span className="sr-only">, done</span>}
          </b>
          <span className="when">{when}</span>
        </li>
      ))}
    </ol>
  )
}
