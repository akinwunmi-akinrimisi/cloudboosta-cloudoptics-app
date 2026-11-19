import { describe, expect, it } from 'vitest'

import { authRedirectUri, routerBasepath } from './paths'

describe('base paths (SITE-13)', () => {
  it('routes under the build base, without its trailing slash', () => {
    expect(routerBasepath('http', '/app/')).toBe('/app')
    expect(routerBasepath(undefined, '/')).toBe('/')
  })

  it('ignores the base in the demo, which routes in the URL hash', () => {
    expect(routerBasepath('demo', '/demo/')).toBe('/')
  })

  it('sends WorkOS back to the app root under its base', () => {
    expect(authRedirectUri('https://cloudoptics.cloudboosta.com', '/app/')).toBe('https://cloudoptics.cloudboosta.com/app/')
    expect(authRedirectUri('http://localhost:5173', '/')).toBe('http://localhost:5173/')
    expect(authRedirectUri('https://x.example', '/app')).toBe('https://x.example/app/')
  })
})
