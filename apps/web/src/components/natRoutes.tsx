import { ExternalLink } from 'lucide-react'

import type { NatRoutesFix } from '../api/types'
import { Commands } from './commands'

/** R10: each route table that still sends traffic to the idle NAT gateway, with how to look at it
 * and how to move its routes. Cloudoptics never changes routes; the customer decides. */
export function NatRoutes({ routes }: { routes: NatRoutesFix }) {
  const count = routes.tables.length
  return (
    <div className="flex flex-col gap-3 rounded-sm border border-border p-4">
      <div>
        <h3 className="text-sm font-semibold">
          {count === 1 ? '1 route table still sends traffic to it' : `${count} route tables still send traffic to it`}
        </h3>
        <p className="text-xs text-muted">
          Check what these subnets need, then point each route at another NAT gateway or remove it. Changing a route is
          your decision: Cloudoptics never changes routes.
        </p>
      </div>
      <ul className="flex flex-col gap-4">
        {routes.tables.map((t) => (
          <li key={t.routeTableId} className="flex flex-col gap-2">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              <a
                href={t.consoleUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 font-mono font-medium underline"
              >
                {t.routeTableId} <ExternalLink size={12} />
              </a>
              {t.vpcId && <span className="text-secondary">VPC {t.vpcId}</span>}
              {t.main && <span className="text-secondary">Main route table</span>}
            </div>
            <p className="text-xs text-secondary">
              Sends {t.destinations.join(', ')} to the gateway, for{' '}
              {t.subnetIds.length > 0
                ? `subnets ${t.subnetIds.join(', ')}`
                : t.main
                  ? 'every subnet without its own route table'
                  : 'no subnet'}
              .
            </p>
            <Commands steps={[t.inspect, ...t.replace]} />
            {t.delete.length > 0 && (
              <details className="text-xs">
                <summary className="cursor-pointer text-secondary">If these subnets need no internet access</summary>
                <div className="mt-2">
                  <Commands steps={t.delete} />
                </div>
              </details>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
