import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DemoApi } from '../api/demo'
import type { Api, ExportJob } from '../api/types'
import { ApiProvider } from '../app/context'
import { ExportButton, ExportList } from './exports'

function job(change: Partial<ExportJob> = {}): ExportJob {
  return {
    id: 'e1',
    kind: 'costs',
    params: {},
    status: 'succeeded',
    progress: 100,
    rowCount: 3,
    fileName: 'cloudoptics-costs-20260901-20260930.csv',
    sizeBytes: 120,
    error: null,
    createdAt: '2026-10-01T10:00:00Z',
    startedAt: '2026-10-01T10:00:00Z',
    finishedAt: '2026-10-01T10:00:01Z',
    expiresAt: '2026-10-08T10:00:00Z',
    downloadUrl: '/v1/tenants/t1/exports/e1/download',
    ...change,
  }
}

function wrap(api: Partial<Api>, children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <ApiProvider api={api as Api}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </ApiProvider>,
  )
}

const REQUEST = { kind: 'costs', start: '2026-09-01', end: '2026-09-30', granularity: 'day' } as const

describe('Export button', () => {
  const saved: string[] = []
  beforeEach(() => {
    saved.length = 0
    URL.createObjectURL = vi.fn(() => 'blob:csv')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      saved.push(this.download)
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('downloads a small export at once', async () => {
    const api = {
      createExport: vi.fn(async () => job()),
      downloadExport: vi.fn(async () => new Blob(['a,b\r\n'])),
    }
    wrap(api, <ExportButton tenantId="t1" request={REQUEST} />)
    await userEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(saved).toEqual(['cloudoptics-costs-20260901-20260930.csv']))
    expect(api.createExport).toHaveBeenCalledWith('t1', REQUEST)
    expect(api.downloadExport).toHaveBeenCalledWith('t1', 'e1')
  })

  it('shows the progress of a large export, then downloads it', async () => {
    let calls = 0
    let release: () => void = () => {}
    const gate = new Promise<void>((r) => (release = r))
    const api = {
      createExport: vi.fn(async () => job({ status: 'queued', progress: 0, downloadUrl: null })),
      getExport: vi.fn(async () => {
        calls += 1
        if (calls === 1) return job({ status: 'running', progress: 41, downloadUrl: null })
        await gate
        return job()
      }),
      downloadExport: vi.fn(async () => new Blob(['a\r\n'])),
    }
    wrap(api, <ExportButton tenantId="t1" request={REQUEST} pollMs={1} />)
    await userEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(await screen.findByRole('button', { name: 'Preparing… 41%' })).toBeTruthy()
    expect(screen.getByRole('progressbar', { name: 'Export progress' }).getAttribute('value')).toBe('41')
    release()
    await waitFor(() => expect(saved).toHaveLength(1))
    expect(await screen.findByRole('button', { name: 'Export CSV' })).toBeTruthy()
    expect(screen.queryByRole('progressbar')).toBeNull()
  })

  it('says why an export failed or was refused', async () => {
    const api = {
      createExport: vi.fn(async () => job({ status: 'queued', progress: 0 })),
      getExport: vi.fn(async () => job({ status: 'failed', error: 'This export is too large to keep.' })),
    }
    wrap(api, <ExportButton tenantId="t1" request={REQUEST} pollMs={1} />)
    await userEvent.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(await screen.findByText('This export is too large to keep.')).toBeTruthy()
    expect(saved).toEqual([])
  })

  it('lists your exports and downloads a ready one again', async () => {
    const api = {
      listExports: vi.fn(async () => ({
        exports: [
          job(),
          job({ id: 'e2', kind: 'recommendations', fileName: 'recs.csv', status: 'running', progress: 50, rowCount: null }),
        ],
      })),
      downloadExport: vi.fn(async () => new Blob(['a\r\n'])),
    }
    wrap(api, <ExportList tenantId="t1" pollMs={60_000} />)
    expect(await screen.findByText('Preparing · 50%')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Download recs.csv' })).toBeNull()
    await userEvent.click(screen.getByRole('button', { name: 'Download cloudoptics-costs-20260901-20260930.csv' }))
    await waitFor(() => expect(saved).toEqual(['cloudoptics-costs-20260901-20260930.csv']))
  })
})

describe('Demo exports', () => {
  beforeEach(() => localStorage.clear())

  async function workspace() {
    const api = new DemoApi(0)
    await api.signIn('demo@example.com')
    const tenant = await api.createTenant({ name: 'Kigali Kloud', countryCode: 'ZA' })
    const setup = await api.createConnection(tenant.id, {
      awsAccountId: '565656565656',
      accountType: 'standalone',
      createCurExport: true,
    })
    await api.validateConnection(tenant.id, setup.connection.id)
    return { api, tenantId: tenant.id }
  }

  it('makes a small CSV from what the demo shows, in both currencies', async () => {
    const { api, tenantId } = await workspace()
    const month = new Date().toISOString().slice(0, 7)
    const made = await api.createExport(tenantId, { kind: 'costs', start: `${month}-01`, end: `${month}-28`, groupBy: ['service'] })
    expect(made.status).toBe('succeeded')
    expect(made.rowCount).toBeGreaterThan(0)
    const csv = await (await api.downloadExport(tenantId, made.id)).text()
    const [header] = csv.replace('﻿', '').split('\r\n')
    expect(header).toBe(
      'period_start,period_end,service_code,service_name,amortised_cost_usd,amortised_cost_zar,fx_rate_avg',
    )
    expect((await api.listExports(tenantId)).exports.map((e) => e.id)).toEqual([made.id])
    const recs = await api.createExport(tenantId, { kind: 'recommendations' })
    expect(await (await api.downloadExport(tenantId, recs.id)).text()).toContain('evidence_summary')
  })

  it('runs daily costs as a large export and limits exports to 10 an hour', async () => {
    const { api, tenantId } = await workspace()
    const slow = new DemoApi(5)
    await slow.signIn('demo@example.com')
    const queued = await slow.createExport(tenantId, { ...REQUEST, granularity: 'day' })
    expect(queued.status).toBe('running')
    await expect(slow.downloadExport(tenantId, queued.id)).rejects.toThrow('not ready')
    await waitFor(async () => expect((await slow.getExport(tenantId, queued.id)).status).toBe('succeeded'))

    for (let i = 0; i < 10; i++) await api.createExport(tenantId, { kind: 'budgets' })
    await expect(api.createExport(tenantId, { kind: 'budgets' })).rejects.toThrow('10 exports an hour')
    await expect(api.getExport('other', queued.id)).rejects.toThrow()
  })
})
