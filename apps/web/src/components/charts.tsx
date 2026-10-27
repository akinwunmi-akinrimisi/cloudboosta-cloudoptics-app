import { formatMoney } from '../lib/currency'

export interface DailyBar {
  date: string
  amount: number
}

function dayLabel(isoDate: string): string {
  return new Date(`${isoDate}T00:00:00Z`).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  })
}

/**
 * Daily spend as bars. Plain SVG keeps the app shell inside its size budget; ECharts
 * (ADR-005) arrives with the Costs Explorer, which needs zoom, stacking and tooltips.
 */
export function DailySpendChart({
  points,
  currency,
  label = 'Daily spend',
}: {
  points: DailyBar[]
  currency: string
  label?: string
}) {
  const width = 600
  const height = 160
  const max = Math.max(...points.map((p) => p.amount), 0)
  const step = width / Math.max(points.length, 1)
  const gap = Math.min(4, step * 0.25)
  const scale = max > 0 ? (height - 4) / max : 0
  const total = points.reduce((sum, p) => sum + p.amount, 0)
  const first = points[0]
  const last = points[points.length - 1]

  return (
    <figure className="flex flex-col gap-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-40 w-full"
        role="img"
        aria-label={`${label}, ${points.length} days, ${formatMoney(total, currency)} in total`}
      >
        <line x1={0} y1={height - 0.5} x2={width} y2={height - 0.5} stroke="var(--border)" />
        {points.map((p, i) => {
          const h = Math.max(p.amount * scale, p.amount > 0 ? 1 : 0)
          return (
            <rect
              key={p.date}
              x={i * step + gap / 2}
              y={height - h}
              width={step - gap}
              height={h}
              rx={1.5}
              fill="var(--accent)"
            >
              <title>{`${dayLabel(p.date)}: ${formatMoney(p.amount, currency)}`}</title>
            </rect>
          )
        })}
      </svg>
      {first && last && (
        <figcaption className="flex justify-between text-xs text-muted">
          <span>{dayLabel(first.date)}</span>
          <span>Highest day {formatMoney(max, currency)}</span>
          <span>{dayLabel(last.date)}</span>
        </figcaption>
      )}
    </figure>
  )
}

export interface UsagePoint {
  date: string
  value: number | null
}

/** Daily usage as bars on a fixed scale (0-100 for percentages), gaps where no data. */
export function UsageChart({
  points,
  unit,
  label,
  max,
}: {
  points: UsagePoint[]
  unit: string
  label: string
  max?: number
}) {
  const width = 600
  const height = 120
  const values = points.map((p) => p.value ?? 0)
  const top = max ?? Math.max(...values, 1)
  const step = width / Math.max(points.length, 1)
  const gap = Math.min(4, step * 0.25)
  const peak = Math.max(...values, 0)
  const first = points[0]
  const last = points[points.length - 1]
  const show = (v: number) => `${Math.round(v * 10) / 10}${unit}`
  return (
    <figure className="flex flex-col gap-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-28 w-full"
        role="img"
        aria-label={`${label}, ${points.length} days, highest ${show(peak)}`}
      >
        <line x1={0} y1={height - 0.5} x2={width} y2={height - 0.5} stroke="var(--border)" />
        {points.map((p, i) => {
          if (p.value === null) return null
          const h = Math.max((p.value / top) * (height - 4), p.value > 0 ? 1 : 0)
          return (
            <rect
              key={p.date}
              x={i * step + gap / 2}
              y={height - h}
              width={step - gap}
              height={h}
              rx={1.5}
              fill="var(--accent)"
            >
              <title>{`${dayLabel(p.date)}: ${show(p.value)}`}</title>
            </rect>
          )
        })}
      </svg>
      {first && last && (
        <figcaption className="flex justify-between text-xs text-muted">
          <span>{dayLabel(first.date)}</span>
          <span>Highest {show(peak)}</span>
          <span>{dayLabel(last.date)}</span>
        </figcaption>
      )}
    </figure>
  )
}

export interface ShareItem {
  key: string
  label: string
  amount: number
  share: number
}

/** A ranked list with a bar per row showing its share of the total. */
export function ShareList({ items, currency }: { items: ShareItem[]; currency: string }) {
  return (
    <ul className="flex flex-col gap-3">
      {items.map((item) => (
        <li key={item.key} className="flex flex-col gap-1">
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="truncate" title={item.label}>
              {item.label}
            </span>
            <span className="shrink-0 tabular-nums">
              {formatMoney(item.amount, currency)}{' '}
              <span className="text-xs text-muted">{Math.round(item.share * 100)}%</span>
            </span>
          </div>
          <div className="h-1.5 rounded-full bg-surface-2">
            <div
              className="h-1.5 rounded-full bg-accent"
              style={{ width: `${Math.max(item.share * 100, 1)}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  )
}

export interface ExpectedPoint {
  date: string
  actual: number
  expected: number | null
  upper: number | null
}

/**
 * Daily cost of one account and service with its expected level (line) and the alert
 * threshold (dashed); the anomaly's day is highlighted.
 */
export function AnomalyChart({ points, highlight, currency }: { points: ExpectedPoint[]; highlight: string; currency: string }) {
  const width = 600
  const height = 160
  const max = Math.max(...points.flatMap((p) => [p.actual, p.upper ?? 0]), 0)
  const step = width / Math.max(points.length, 1)
  const gap = Math.min(4, step * 0.25)
  const scale = max > 0 ? (height - 4) / max : 0
  const y = (v: number) => height - v * scale
  const line = (key: 'expected' | 'upper') =>
    points
      .map((p, i) => (p[key] === null ? null : `${i * step + step / 2},${y(p[key] as number)}`))
      .filter(Boolean)
      .join(' ')
  const first = points[0]
  const last = points[points.length - 1]
  return (
    <figure className="flex flex-col gap-2">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        className="h-40 w-full"
        role="img"
        aria-label={`Daily cost for ${points.length} days against the expected level`}
      >
        <line x1={0} y1={height - 0.5} x2={width} y2={height - 0.5} stroke="var(--border)" />
        {points.map((p, i) => {
          const h = Math.max(p.actual * scale, p.actual > 0 ? 1 : 0)
          return (
            <rect
              key={p.date}
              x={i * step + gap / 2}
              y={height - h}
              width={step - gap}
              height={h}
              rx={1.5}
              fill={p.date === highlight ? 'var(--critical-ink)' : 'var(--accent)'}
              opacity={p.date === highlight ? 1 : 0.55}
            >
              <title>
                {`${dayLabel(p.date)}: ${formatMoney(p.actual, currency)}` +
                  (p.expected === null ? '' : `, expected ${formatMoney(p.expected, currency)}`)}
              </title>
            </rect>
          )
        })}
        <polyline points={line('expected')} fill="none" stroke="var(--text-primary)" strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        <polyline
          points={line('upper')}
          fill="none"
          stroke="var(--warning-ink)"
          strokeWidth={1.5}
          strokeDasharray="4 3"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      {first && last && (
        <figcaption className="flex flex-wrap justify-between gap-2 text-xs text-muted">
          <span>{dayLabel(first.date)}</span>
          <span>Bars: daily cost · line: expected · dashed: alert level</span>
          <span>{dayLabel(last.date)}</span>
        </figcaption>
      )}
    </figure>
  )
}
