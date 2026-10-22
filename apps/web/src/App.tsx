import { useState } from 'react'

import { type CurrencyMode, formatDual } from './lib/currency'

// Sprint 0 placeholder: proves the toolchain and the currency toggle end to end.
// Routing, data fetching and the design-system components arrive in Sprint 1 (FE-1).
const SAMPLE_RATE = { currency: 'NGN', rate: 1500 }
const MODES: CurrencyMode[] = ['usd', 'local', 'both']
const LABELS: Record<CurrencyMode, string> = { usd: 'USD', local: 'Local', both: 'Both' }

export default function App() {
  const [mode, setMode] = useState<CurrencyMode>('both')
  return (
    <main className="shell">
      <header className="brand">
        <img src="/favicon.svg" alt="" width={32} height={32} />
        <span>Cloudoptics</span>
      </header>
      <h1>Month-to-date spend</h1>
      <p className="kpi" data-testid="mtd">
        {formatDual(18420.5, mode, SAMPLE_RATE)}
      </p>
      <div role="group" aria-label="Currency" className="toggle">
        {MODES.map((m) => (
          <button key={m} type="button" aria-pressed={mode === m} onClick={() => setMode(m)}>
            {LABELS[m]}
          </button>
        ))}
      </div>
      <p className="note">Sample figures. Rate set by Cloudoptics staff (illustrative).</p>
    </main>
  )
}
