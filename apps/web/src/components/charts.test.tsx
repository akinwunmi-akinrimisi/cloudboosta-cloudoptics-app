import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AnomalyChart, UsageChart } from './charts'

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
    // The table alternative (FR-UI-06) says "No data" where the chart has a gap.
    const table = screen.getByRole('table', { name: 'Highest CPU use each day' })
    expect(table.querySelectorAll('tbody tr')).toHaveLength(3)
    expect(screen.getByRole('rowheader', { name: '2 Oct' }).nextElementSibling?.textContent).toBe('No data')
    expect(screen.getByText('Show highest CPU use each day as a table')).toBeTruthy()
  })
})

describe('AnomalyChart', () => {
  it('lists cost, expected level and alert level per day, marking the anomaly', () => {
    render(
      <AnomalyChart
        currency="USD"
        highlight="2026-10-02"
        points={[
          { date: '2026-10-01', actual: 10, expected: 10, upper: 14 },
          { date: '2026-10-02', actual: 40, expected: 10, upper: 14 },
        ]}
      />,
    )
    const table = screen.getByRole('table', { name: 'Daily cost against the expected level' })
    expect([...table.querySelectorAll('thead th')].map((th) => th.textContent)).toEqual(['Day', 'Cost', 'Expected', 'Alert level'])
    expect(screen.getByRole('rowheader', { name: '2 Oct (anomaly)' })).toBeTruthy()
  })
})
