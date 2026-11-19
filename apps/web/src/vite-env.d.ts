/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_MODE?: 'demo' | 'http'
  readonly VITE_API_URL?: string
  /** Path the build is served from: /app/ on staging, /demo/ for the demo (vite.config.ts). */
  readonly VITE_BASE?: string
  /** "Start free" target in the demo banner (default /app/sign-in?intent=signup). */
  readonly VITE_SIGNUP_URL?: string
  /** The public demo, linked from sign-in (default /demo/). */
  readonly VITE_DEMO_URL?: string
}
