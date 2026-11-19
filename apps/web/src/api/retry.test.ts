import { describe, expect, it } from 'vitest'

import { retryDelay, retryQuery } from './retry'
import { ApiError } from './types'

describe('query retries', () => {
  it('retries other errors once', () => {
    const error = new ApiError(500, 'boom')
    expect(retryQuery(0, error)).toBe(true)
    expect(retryQuery(1, error)).toBe(false)
    expect(retryDelay(0, error)).toBe(1000)
  })

  it('waits for Retry-After on a 429 and gives up on long waits', () => {
    const short = new ApiError(429, 'slow down', [], 'rate_limited', 5)
    expect(retryQuery(0, short)).toBe(true)
    expect(retryDelay(0, short)).toBe(5000)
    expect(retryQuery(1, short)).toBe(false)
    expect(retryQuery(0, new ApiError(429, 'slow down', [], 'rate_limited', 50))).toBe(false)
    expect(retryQuery(0, new ApiError(429, 'slow down'))).toBe(false)
  })
})
