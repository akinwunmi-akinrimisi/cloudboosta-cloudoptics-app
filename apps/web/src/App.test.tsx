import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from './App'

describe('App', () => {
  it('switches between USD, Local and Both', () => {
    render(<App />)
    const kpi = screen.getByTestId('mtd')
    expect(kpi.textContent).toBe('$18,420.50 · ₦27,630,750.00')

    fireEvent.click(screen.getByRole('button', { name: 'USD' }))
    expect(kpi.textContent).toBe('$18,420.50')
    expect(screen.getByRole('button', { name: 'USD' }).getAttribute('aria-pressed')).toBe('true')

    fireEvent.click(screen.getByRole('button', { name: 'Local' }))
    expect(kpi.textContent).toBe('₦27,630,750.00')
  })
})
