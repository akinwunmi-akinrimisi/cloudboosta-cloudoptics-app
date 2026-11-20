import { useQuery } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'

import { useApi } from '../app/context'
import type { Connection } from '../api/types'
import { ApiError } from '../api/types'
import { Alert } from '../components/ui'
import { connectionLabel } from '../lib/providers'
import { CopyBlock } from './connect/AzureConnect'

const LINK_BUTTON =
  'inline-flex min-h-9 items-center justify-center gap-2 rounded-sm bg-accent px-4 py-1 text-sm font-medium text-on-accent hover:bg-accent-hover'

/**
 * "Set up again" for an Azure connection, on its card: the consent link, the Cloud Shell
 * script and the ownership code, from the API (fetched only when opened). After Reconnect the
 * code is new, because disconnecting deleted the old one.
 */
export default function AzureSetupAgain({
  tenantId,
  connection: c,
  newCode,
  id,
}: {
  tenantId: string
  connection: Connection
  /** Just reconnected: say the code changed, so the script is run again. */
  newCode: boolean
  /** For the button's aria-controls. */
  id: string
}) {
  const api = useApi()
  const setup = useQuery({
    queryKey: ['azure-setup', tenantId, c.id],
    queryFn: () => api.getAzureConnection(tenantId, c.id),
    retry: false,
  })
  const s = setup.data
  return (
    <section id={id} aria-label="Set up access in Azure again" className="flex min-w-0 flex-col gap-3 rounded-sm bg-surface-2 p-3 text-sm">
      <div>
        <p className="font-medium">Set up access in Azure again</p>
        <p className="break-words text-secondary">
          For {connectionLabel(c)}. Do these steps in Azure, then choose Check again. Your costs and history stay.
        </p>
      </div>
      {setup.isLoading && <p className="text-secondary">Loading…</p>}
      {setup.error && (
        <Alert tone="error">{setup.error instanceof ApiError ? setup.error.message : 'Could not load the steps.'}</Alert>
      )}
      {s && (
        <>
          {newCode && (
            <Alert tone="info">
              Disconnecting deleted the old ownership code, so this connection has a new one. Run the script again: it
              sets the new code on the storage account.
            </Alert>
          )}
          <div className="flex flex-col gap-2">
            <p className="font-medium">1. Grant admin consent</p>
            <p className="break-words text-secondary">
              A Global Administrator or Privileged Role Administrator of directory{' '}
              <code className="font-mono text-xs break-all">{s.connection.directoryId}</code> approves the Cloudoptics
              app. It asks only to sign in and read.
            </p>
            <div>
              <a href={s.consentUrl} target="_blank" rel="noreferrer" className={LINK_BUTTON}>
                Grant admin consent <ExternalLink size={16} aria-hidden="true" />
                <span className="sr-only">(opens in a new tab)</span>
              </a>
            </div>
          </div>
          <div className="flex min-w-0 flex-col gap-2">
            <p className="font-medium">2. Run the script in Azure Cloud Shell (Bash)</p>
            <p className="text-secondary">
              It adds the read-only roles and tags the export&apos;s storage account with your ownership code.
            </p>
            <CopyBlock label="Azure CLI command" value={s.roleAssignmentCommand} />
            <CopyBlock label={`Ownership code (tag ${s.ownershipTag})`} value={s.ownershipCode} />
          </div>
        </>
      )}
    </section>
  )
}
