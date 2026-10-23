import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { UsageChart } from './charts'

describe('UsageChart', () => {
  it('draws a bar per day with data and names the peak', () => {
    const { container } = render(
      <UsageChart
        label="Highest CPU use each day"
        unit="%"
        max={100}
        points={[
          { date: '2026-10-01', value: 2.04 },
          { date: '2026-10-02', value: null },
          { date: '2026-10-03', value: 4.5 },
        ]}
      />,
    )
    expect(screen.getByRole('img', { name: 'Highest CPU use each day, 3 days, highest 4.5%' })).toBeTruthy()
    expect(container.querySelectorAll('rect')).toHaveLength(2)
    expect(screen.getByText('Highest 4.5%')).toBeTruthy()
  })
})
