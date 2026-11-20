import { beforeEach, describe, expect, it } from 'vitest'

import { DemoApi } from './demo'
import { scopedApi } from './handoff'
import { ApiError } from './types'

describe('a connect-invite session in the browser', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  async function invited() {
    const api = new DemoApi(0)
    await api.signIn('ada@example.com')
    const tenant = await api.createTenant({ name: 'Lagos Ledger', countryCode: 'NG' })
    const invite = await api.sendConnectInvite(tenant.id, 'ops@example.com', 'gcp')
    const token = (invite.demoLink ?? '').split('/').pop() as string
    const opened = await api.openConnectInvite(token)
    return { api, tenant, invite, session: api.inviteSession(opened.sessionToken), opened }
  }

  it('reaches only the connect calls; everything else is refused', async () => {
    const { api, tenant, session, opened } = await invited()
    expect(opened.invite).toMatchObject({ provider: 'gcp', status: 'opened', workspaceName: 'Lagos Ledger' })
    const scoped = scopedApi(session, api)
    for (const call of [
      () => scoped.costSummary(tenant.id),
      () => scoped.listMembers(tenant.id),
      () => scoped.listRecommendations(tenant.id),
      () => scoped.getTenant(tenant.id),
      () => scoped.createExport(tenant.id, { kind: 'costs' } as never),
    ]) {
      await expect(call()).rejects.toSatisfy((e: unknown) => e instanceof ApiError && e.status === 403)
    }
    expect(await scoped.listConnections(tenant.id)).toEqual([])
    expect(scoped.isSignedIn()).toBe(false)
  })

  it('shows sent, opened, then withdrawn to the sender', async () => {
    const { api, tenant, invite } = await invited()
    expect((await api.listConnectInvites(tenant.id)).map((i) => i.status)).toEqual(['opened'])
    await api.revokeConnectInvite(tenant.id, invite.id)
    expect((await api.listConnectInvites(tenant.id)).map((i) => i.status)).toEqual(['revoked'])
    const token = (invite.demoLink ?? '').split('/').pop() as string
    await expect(api.openConnectInvite(token)).rejects.toSatisfy((e: unknown) => e instanceof ApiError && e.status === 410)
  })
})
