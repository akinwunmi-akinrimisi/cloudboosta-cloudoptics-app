import { DemoApi } from './demo'
import { HttpApi } from './http'
import type { Api } from './types'

// VITE_API_MODE=demo builds the self-contained GitHub Pages version; otherwise the app
// calls the API at VITE_API_URL (default: the local API).
export function createApi(): Api {
  if (import.meta.env.VITE_API_MODE === 'demo') return new DemoApi()
  return new HttpApi(import.meta.env.VITE_API_URL ?? 'http://localhost:8000')
}

export const api: Api = createApi()
