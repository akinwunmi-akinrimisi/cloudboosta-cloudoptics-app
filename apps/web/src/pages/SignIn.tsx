import { useNavigate } from '@tanstack/react-router'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import type { FormEvent } from 'react'
import { useState } from 'react'

import { ApiError } from '../api/types'
import { useApi } from '../app/context'
import { Alert, Button, Card, Field, Logo } from '../components/ui'

export function SignIn() {
  const api = useApi()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [email, setEmail] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const config = useQuery({ queryKey: ['auth-config'], queryFn: () => api.authConfig() })
  const pending = config.data?.mode === 'oidc' && !config.data.configured
  const external = config.data?.mode === 'oidc' && config.data.configured

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.signIn(email.trim(), name.trim() || undefined)
      await qc.invalidateQueries()
      await navigate({ to: '/' })
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Sign-in failed. Try again.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2 text-lg font-semibold">
          <Logo /> Cloudoptics
        </div>
        <Card>
          <h1 className="text-2xl font-semibold">Sign in</h1>
          <p className="mt-1 text-sm text-secondary">
            See your AWS spend in dollars and your local currency, and what is safe to cut.
          </p>
          {pending && (
            <div className="mt-6">
              <Alert tone="info">
                Sign-in is being set up for this environment. Try the{' '}
                <a
                  className="font-medium text-accent underline"
                  href="https://akinwunmi-akinrimisi.github.io/cloudboosta-cloudoptics-app/"
                >
                  public demo
                </a>{' '}
                in the meantime.
              </Alert>
            </div>
          )}
          {external && (
            <div className="mt-6 flex flex-col gap-3">
              {error && <Alert tone="error">{error}</Alert>}
              <Button
                disabled={busy}
                onClick={() => {
                  setBusy(true)
                  api.signIn('').catch(() => {
                    setError('Sign-in could not start. Try again.')
                    setBusy(false)
                  })
                }}
              >
                {busy ? 'Opening sign-in…' : 'Continue to sign in'}
              </Button>
              <p className="text-xs text-muted">Email, Google or single sign-on, with multi-factor authentication.</p>
            </div>
          )}
          {!pending && !external && (
            <form className="mt-6 flex flex-col gap-4" onSubmit={submit}>
              <Field
                label="Work email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
              <Field
                label="Your name (optional)"
                autoComplete="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              {error && <Alert tone="error">{error}</Alert>}
              <Button type="submit" disabled={busy}>
                {busy ? 'Signing in…' : 'Continue'}
              </Button>
            </form>
          )}
          {pending || external ? null : api.mode === 'demo' ? (
            <p className="mt-4 text-xs text-muted">
              Demo: no password, no AWS calls. Everything you enter stays in this browser.
            </p>
          ) : (
            <p className="mt-4 text-xs text-muted">
              Development sign-in. Production uses single sign-on with MFA.
            </p>
          )}
        </Card>
      </div>
    </main>
  )
}
