import type { ComponentType } from 'react'

import type { Provider } from '../../api/types'
import { PROVIDER_LABEL } from '../../lib/providers'
// Onboarding imports this list too. The cycle is safe: ConnectNew is a hoisted function
// declaration and Onboarding reads CONNECT_PROVIDERS only when it renders.
import { ConnectNew } from '../Onboarding'
import type { ConnectProps } from './GcpConnect'
import { GcpConnect } from './GcpConnect'
import { DigitalOceanConnect } from './DigitalOceanConnect'

export type { ConnectProps }

export interface ConnectProvider {
  id: Provider
  label: string
  /** One short line under the label in the "Which cloud?" picker. */
  description: string
  /** The connect step: calls onConnected with the new connection's ID when access is set up. */
  Component: ComponentType<ConnectProps>
}

/** AWS: the default, and what the connect step shows when nothing else is chosen. */
export const DEFAULT_CONNECT: ConnectProvider = {
  id: 'aws',
  label: PROVIDER_LABEL.aws,
  description: 'One click in AWS gives read-only access.',
  Component: ConnectNew,
}

/**
 * The clouds a workspace can connect, in the order the picker shows them (the first is the
 * default). Register a new provider here and nowhere else.
 */
export const CONNECT_PROVIDERS: ConnectProvider[] = [
  DEFAULT_CONNECT,
  {
    id: 'gcp',
    label: PROVIDER_LABEL.gcp,
    description: 'Your billing export in BigQuery.',
    Component: GcpConnect,
  },
  {
    id: 'digitalocean',
    label: PROVIDER_LABEL.digitalocean,
    description: 'A read-only API token you paste once.',
    Component: DigitalOceanConnect,
  },
]
