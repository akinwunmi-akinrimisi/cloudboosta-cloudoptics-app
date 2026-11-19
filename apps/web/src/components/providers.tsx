import type { Provider } from '../api/types'
import { PROVIDER_LABEL, providerOf } from '../lib/providers'

/**
 * The provider's name as a small text badge, shown only when the workspace has more than one
 * provider. AWS-only workspaces render nothing, so they read exactly as before.
 */
export function ProviderBadge({ provider, show }: { provider?: Provider | null; show: boolean }) {
  if (!show) return null
  return (
    <span className="inline-flex shrink-0 rounded-full bg-surface-2 px-2 py-0.5 align-middle text-xs font-medium text-secondary">
      {PROVIDER_LABEL[providerOf(provider)]}
    </span>
  )
}
