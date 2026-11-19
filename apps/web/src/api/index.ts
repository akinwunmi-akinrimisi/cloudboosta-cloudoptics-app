import { DemoApi } from './demo'
import { HttpApi } from './http'
import type { Api } from './types'

// VITE_API_MODE=demo builds the self-contained demo (served at /demo/, mirrored on GitHub
// Pages); otherwise the app calls the API at VITE_API_URL (default: the local API).
export function createApi(): Api {
  if (import.meta.env.VITE_API_MODE === 'demo') {
    const demo = new DemoApi()
    // A first visit opens the sample workspace straight away (SITE-14).
    demo.openSample()
    return demo
  }
  return new HttpApi(import.meta.env.VITE_API_URL ?? 'http://localhost:8000')
}

export const api: Api = createApi()
