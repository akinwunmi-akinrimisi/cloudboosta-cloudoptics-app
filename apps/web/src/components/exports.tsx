import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { useState } from 'react'

import { useApi } from '../app/context'
import type { ExportJob, ExportRequest } from '../api/types'
import { saveFile } from '../lib/csv'
import { Button, cx } from './ui'

const POLL_MS = 1500

/**
 * Exports what the page shows to CSV (PRD US-7.2). A small export downloads at once; a large one runs on
 * the worker: the button shows its progress, then downloads it. Your exports page keeps it for 7 days.
 */
export function ExportButton({
  tenantId,
  request,
  label = 'Export CSV',
  pollMs = POLL_MS,
  className,
}: {
  tenantId: string
  request: ExportRequest
  label?: string
  pollMs?: number
  className?: string
}) {
  const api = useApi()
  const qc = useQueryClient()
  // Null until a queued export reports progress.
  const [progress, setProgress] = useState<number | null>(null)
  const run = useMutation({
    mutationFn: async () => {
      let job = await api.createExport(tenantId, request)
      void qc.invalidateQueries({ queryKey: ['exports', tenantId] })
      while (job.status === 'queued' || job.status === 'running') {
        setProgress(job.progress)
        await new Promise((r) => setTimeout(r, pollMs))
        job = await api.getExport(tenantId, job.id)
      }
      setProgress(null)
      if (job.status !== 'succeeded') throw new Error(job.error ?? 'The export failed. Try again.')
      saveFile(await api.downloadExport(tenantId, job.id), job.fileName)
      return job
    },
    onSettled: () => {
      setProgress(null)
      void qc.invalidateQueries({ queryKey: ['exports', tenantId] })
    },
  })
  const error = run.error?.message ?? null

  return (
    <span className={cx('inline-flex flex-col items-end gap-1', className)}>
      <Button variant="secondary" className="h-8 px-3 text-xs" disabled={run.isPending} onClick={() => run.mutate()}>
        <Download size={14} />
        {progress !== null ? `Preparing… ${progress}%` : run.isPending ? 'Exporting…' : label}
      </Button>
      {progress !== null && (
        <progress className="h-1 w-full" max={100} value={progress} aria-label="Export progress">
          {progress}%
        </progress>
      )}
      <span aria-live="polite" className="text-xs text-critical-ink">
        {error}
      </span>
    </span>
  )
}

const STATUS: Record<ExportJob['status'], string> = {
  queued: 'Waiting',
  running: 'Preparing',
  succeeded: 'Ready',
  failed: 'Failed',
  expired: 'Expired',
}

const KIND: Record<ExportJob['kind'], string> = {
  costs: 'Costs',
  recommendations: 'Recommendations',
  anomalies: 'Anomalies',
  budgets: 'Budgets',
}

/** Your exports from the last 7 days, to download again or once a large one is ready. */
export function ExportList({ tenantId, pollMs = POLL_MS }: { tenantId: string; pollMs?: number }) {
  const api = useApi()
  const list = useQuery({
    queryKey: ['exports', tenantId],
    queryFn: () => api.listExports(tenantId),
    refetchInterval: (q) =>
      q.state.data?.exports.some((e) => e.status === 'queued' || e.status === 'running') ? pollMs : false,
  })
  const save = useMutation({
    mutationFn: async (job: ExportJob) => saveFile(await api.downloadExport(tenantId, job.id), job.fileName),
  })
  const exports = list.data?.exports ?? []
  return (
    <section aria-labelledby="exports-heading" className="rounded-md border border-border bg-surface-1 p-5 print:hidden">
      <h2 id="exports-heading" className="text-base font-semibold">
        Your exports
      </h2>
      <p className="mb-3 text-xs text-muted">
        CSV files you exported in the last 7 days. Large exports are prepared in the background and appear here.
      </p>
      {list.error && <p className="text-sm text-critical-ink">{list.error.message}</p>}
      {save.error && <p className="text-sm text-critical-ink">{save.error.message}</p>}
      {list.data && exports.length === 0 && <p className="text-sm text-secondary">No exports yet.</p>}
      {exports.length > 0 && (
        <div className="overflow-x-auto" role="region" aria-label="Export files" tabIndex={0}>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted">
              <th className="py-2 font-medium">File</th>
              <th className="py-2 font-medium">What</th>
              <th className="py-2 font-medium">Status</th>
              <th className="py-2 text-right font-medium">Rows</th>
              <th className="py-2">
                <span className="sr-only">Download</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {exports.map((e) => (
              <tr key={e.id} className="border-b border-border last:border-0">
                <td className="max-w-0 truncate py-2 pr-3" title={e.fileName}>
                  {e.fileName}
                </td>
                <td className="py-2 pr-3">{KIND[e.kind]}</td>
                <td className="py-2 pr-3">
                  {STATUS[e.status]}
                  {(e.status === 'queued' || e.status === 'running') && ` · ${e.progress}%`}
                  {e.status === 'failed' && e.error && <span className="block text-xs text-muted">{e.error}</span>}
                </td>
                <td className="py-2 text-right tabular-nums">{e.rowCount?.toLocaleString() ?? '—'}</td>
                <td className="py-2 pl-3 text-right">
                  {e.status === 'succeeded' && (
                    <Button
                      variant="ghost"
                      className="h-7 px-2 text-xs"
                      aria-label={`Download ${e.fileName}`}
                      disabled={save.isPending}
                      onClick={() => save.mutate(e)}
                    >
                      <Download size={14} /> Download
                    </Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
    </section>
  )
}
