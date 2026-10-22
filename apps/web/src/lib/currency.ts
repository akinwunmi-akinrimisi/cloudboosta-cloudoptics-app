/**
 * Money formatting for the USD / Local / Both toggle (docs/08 §2.2, PRD Epic E3).
 *
 * Costs are stored in USD. Local values are USD × the admin-set rate for the charge date
 * (FR-FX-01/02); a rate taken from the reference feed is flagged as estimated and shown
 * with "≈" so it is never mistaken for a staff-approved figure.
 */

export type CurrencyMode = 'usd' | 'local' | 'both'

export interface FxRate {
  /** ISO 4217 code of the local currency, e.g. "NGN". */
  currency: string
  /** Units of local currency per 1 USD. */
  rate: number
  /** True when the value comes from the feed rather than a manual staff rate. */
  estimated?: boolean
}

export interface FormatOptions {
  compact?: boolean
  locale?: string
}

const formatters = new Map<string, Intl.NumberFormat>()

function formatter(currency: string, { compact = false, locale = 'en' }: FormatOptions) {
  const key = `${locale}|${currency}|${compact}`
  let f = formatters.get(key)
  if (!f) {
    f = new Intl.NumberFormat(locale, {
      style: 'currency',
      currency,
      currencyDisplay: 'narrowSymbol',
      notation: compact ? 'compact' : 'standard',
      maximumFractionDigits: compact ? 1 : 2,
      minimumFractionDigits: compact ? 0 : 2,
    })
    formatters.set(key, f)
  }
  return f
}

export function formatMoney(amount: number, currency: string, options: FormatOptions = {}): string {
  return formatter(currency, options).format(amount)
}

/** Convert USD to local currency, rounded to 2 decimal places (half away from zero). */
export function toLocal(usd: number, fx: FxRate): number {
  if (!(fx.rate > 0)) throw new RangeError(`Invalid exchange rate for ${fx.currency}: ${fx.rate}`)
  const value = usd * fx.rate
  return Math.sign(value) * Math.round(Math.abs(value) * 100) / 100
}

export function formatDual(
  usd: number,
  mode: CurrencyMode,
  fx: FxRate | null,
  options: FormatOptions = {},
): string {
  const usdText = formatMoney(usd, 'USD', options)
  if (mode === 'usd' || fx === null) return usdText
  const localText = `${fx.estimated ? '≈' : ''}${formatMoney(toLocal(usd, fx), fx.currency, options)}`
  return mode === 'local' ? localText : `${usdText} · ${localText}`
}
