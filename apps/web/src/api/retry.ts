import { isRateLimited } from './types'

/** Waits longer than this are left to the person (the error says when to try again). */
const MAX_RATE_LIMIT_WAIT_SECONDS = 30

/** TanStack Query ``retry``: one retry, except a 429 retries once only when the wait is short. */
export function retryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false
  if (isRateLimited(error)) return (error.retryAfter ?? Infinity) <= MAX_RATE_LIMIT_WAIT_SECONDS
  return true
}

/** TanStack Query ``retryDelay``: the API's ``Retry-After`` on a 429, else the usual backoff. */
export function retryDelay(attempt: number, error: unknown): number {
  if (isRateLimited(error) && error.retryAfter !== undefined) return error.retryAfter * 1000
  return Math.min(1000 * 2 ** attempt, 30_000)
}
