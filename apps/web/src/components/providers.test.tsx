import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { workspaceProviders } from '../lib/providers'
import { ProviderBadge } from './providers'

describe('ProviderBadge', () => {
  it('renders nothing in an AWS-only workspace', () => {
    const items = [{ provider: 'aws' as const }, {}]
    const { container } = render(<ProviderBadge provider="aws" show={workspaceProviders(items).length > 1} />)
    expect(container.innerHTML).toBe('')
  })

  it('shows the provider as text when two providers exist', () => {
    const items = [{ provider: 'aws' as const }, { provider: 'gcp' as const }]
    const show = workspaceProviders(items).length > 1
    render(
      <>
        <ProviderBadge provider="gcp" show={show} />
        <ProviderBadge show={show} />
      </>,
    )
    expect(screen.getByText('Google Cloud').tagName).toBe('SPAN')
    expect(screen.getByText('AWS').tagName).toBe('SPAN')
  })
})
