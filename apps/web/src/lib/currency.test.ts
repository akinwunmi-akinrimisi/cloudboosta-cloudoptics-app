import { describe, expect, it } from 'vitest'

import { formatDual, formatMoney, toLocal } from './currency'

// Intl inserts non-breaking spaces for some currencies; compare with plain spaces.
const plain = (s: string) => s.replace(/ /g, ' ')

describe('formatMoney', () => {
  it.each([
    ['USD', '$1,234,567.89'],
    ['NGN', '₦1,234,567.89'],
    ['KES', 'KES 1,234,567.89'],
    ['GHS', 'GH₵1,234,567.89'],
    ['ZAR', 'R 1,234,567.89'],
    ['EGP', 'E£1,234,567.89'],
  ])('formats %s', (currency, expected) => {
    expect(plain(formatMoney(1234567.891, currency))).toBe(expected)
  })

  it('formats compact values for tiles', () => {
    expect(formatMoney(1_234_567, 'NGN', { compact: true })).toBe('₦1.2M')
  })

  it('keeps the sign on credits', () => {
    expect(formatMoney(-5, 'NGN')).toBe('-₦5.00')
  })
})

describe('toLocal', () => {
  it('converts and rounds to 2 decimals', () => {
    expect(toLocal(10.005, { currency: 'NGN', rate: 1000 })).toBe(10005)
    expect(toLocal(1.235, { currency: 'KES', rate: 1 })).toBe(1.24)
    expect(toLocal(-1.235, { currency: 'KES', rate: 1 })).toBe(-1.24)
  })

  it('rejects zero, negative and missing rates', () => {
    expect(() => toLocal(1, { currency: 'NGN', rate: 0 })).toThrow(RangeError)
    expect(() => toLocal(1, { currency: 'NGN', rate: -1 })).toThrow(RangeError)
    expect(() => toLocal(1, { currency: 'NGN', rate: Number.NaN })).toThrow(RangeError)
  })
})

describe('formatDual', () => {
  const ngn = { currency: 'NGN', rate: 1500 }

  it('shows USD only, local only, or both', () => {
    expect(formatDual(100, 'usd', ngn)).toBe('$100.00')
    expect(formatDual(100, 'local', ngn)).toBe('₦150,000.00')
    expect(formatDual(100, 'both', ngn)).toBe('$100.00 · ₦150,000.00')
  })

  it('falls back to USD when no rate exists, never inventing one', () => {
    expect(formatDual(100, 'both', null)).toBe('$100.00')
  })

  it('marks feed-based rates as estimated', () => {
    expect(formatDual(100, 'local', { ...ngn, estimated: true })).toBe('≈₦150,000.00')
  })
})
