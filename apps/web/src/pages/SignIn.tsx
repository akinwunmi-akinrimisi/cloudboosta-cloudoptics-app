import { useNavigate } from '@tanstack/react-router'
import { useQueryClient } from '@tanstack/react-query'
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
          {api.mode === 'demo' ? (
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
