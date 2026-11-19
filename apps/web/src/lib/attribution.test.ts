import { afterEach, describe, expect, it, vi } from 'vitest'

import { captureAttribution, clearAttribution, readAttribution } from './attribution'

afterEach(() => {
  clearAttribution()
  vi.unstubAllGlobals()
  Object.defineProperty(navigator, 'doNotTrack', { value: null, configurable: true })
})

describe('attribution', () => {
  it('keeps the campaign and landing path from the sign-in URL', () => {
    captureAttribution('?intent=signup&utm_source=linkedin&utm_medium=social&utm_campaign=beta&lp=%2Fsecurity&code=x')
    expect(readAttribution()).toEqual({ source: 'linkedin', medium: 'social', campaign: 'beta', landingPath: '/security' })
  })

  it('keeps the first touch', () => {
    captureAttribution('?ref=www.google.com&lp=%2F')
    captureAttribution('?utm_source=newsletter')
    expect(readAttribution()).toEqual({ ref: 'www.google.com', landingPath: '/' })
  })

  it('keeps nothing without a campaign, or when the browser asks not to be tracked', () => {
    captureAttribution('?intent=signup&lp=%2F')
    expect(readAttribution()).toBeUndefined()
    Object.defineProperty(navigator, 'doNotTrack', { value: '1', configurable: true })
    captureAttribution('?utm_source=x')
    expect(readAttribution()).toBeUndefined()
  })

  it('caps long values', () => {
    captureAttribution(`?utm_source=${'a'.repeat(500)}`)
    expect(readAttribution()?.source).toHaveLength(100)
  })
})
