import { describe, expect, it } from 'vitest'

import {
  PROVIDER_LABEL,
  accountLabel,
  accountNoun,
  asProvider,
  cloudWords,
  connectionKind,
  connectionLabel,
  providerOf,
  supports,
  workspaceProviders,
} from './providers'

describe('PROVIDER_LABEL', () => {
  it('names every provider', () => {
    expect(PROVIDER_LABEL).toEqual({ aws: 'AWS', gcp: 'Google Cloud', azure: 'Azure', digitalocean: 'DigitalOcean' })
  })
})

describe('providerOf', () => {
  it('defaults a missing provider to AWS', () => {
    expect(providerOf(undefined)).toBe('aws')
    expect(providerOf(null)).toBe('aws')
    expect(providerOf('gcp')).toBe('gcp')
  })
})

describe('accountNoun', () => {
  it.each([
    ['aws', 'account'],
    ['gcp', 'project'],
    ['azure', 'subscription'],
    ['digitalocean', 'team'],
  ] as const)('%s calls it a(n) %s', (provider, noun) => {
    expect(accountNoun(provider)).toBe(noun)
  })

  it('treats a missing provider as AWS', () => {
    expect(accountNoun()).toBe('account')
  })
})

describe('accountLabel', () => {
  it('reads exactly as before for AWS', () => {
    expect(accountLabel('aws', '123456789012')).toBe('AWS account 123456789012')
    expect(accountLabel(undefined, '123456789012')).toBe('AWS account 123456789012')
  })

  it('uses each provider’s own noun', () => {
    expect(accountLabel('gcp', 'my-proj')).toBe('Google Cloud project my-proj')
    expect(accountLabel('azure', 'sub-1')).toBe('Azure subscription sub-1')
    expect(accountLabel('digitalocean', 'team-1')).toBe('DigitalOcean team team-1')
  })
})

describe('workspaceProviders', () => {
  it('is empty for no items', () => {
    expect(workspaceProviders([])).toEqual([])
  })

  it('counts a missing provider as AWS and drops duplicates', () => {
    expect(workspaceProviders([{}, { provider: 'aws' }, { provider: null }])).toEqual(['aws'])
  })

  it('lists distinct providers in a stable order', () => {
    expect(workspaceProviders([{ provider: 'azure' }, {}, { provider: 'gcp' }, { provider: 'azure' }])).toEqual([
      'aws',
      'gcp',
      'azure',
    ])
  })
})

describe('asProvider', () => {
  it('accepts known providers only', () => {
    expect(asProvider('gcp')).toBe('gcp')
    expect(asProvider('oracle')).toBeUndefined()
    expect(asProvider(3)).toBeUndefined()
    expect(asProvider(undefined)).toBeUndefined()
  })
})

describe('connectionLabel', () => {
  it('names a Google Cloud connection by its billing account', () => {
    expect(connectionLabel({ provider: 'gcp', awsAccountId: '012345-6789AB-CDEF01' })).toBe(
      'Google Cloud billing account 012345-6789AB-CDEF01',
    )
  })

  it('names other connections by their account', () => {
    expect(connectionLabel({ awsAccountId: '123456789012' })).toBe('AWS account 123456789012')
    expect(connectionLabel({ provider: 'aws', awsAccountId: '123456789012', accountType: 'management' })).toBe(
      'AWS account 123456789012',
    )
  })

  it('names an Azure connection by its subscription or billing account, DigitalOcean by its team', () => {
    expect(connectionLabel({ provider: 'azure', awsAccountId: 'sub-1', accountType: 'standalone' })).toBe(
      'Azure subscription sub-1',
    )
    expect(connectionLabel({ provider: 'azure', awsAccountId: '1234:5678', accountType: 'management' })).toBe(
      'Azure billing account 1234:5678',
    )
    expect(connectionLabel({ provider: 'digitalocean', awsAccountId: 'team-1' })).toBe('DigitalOcean team team-1')
    expect(connectionKind({ provider: 'gcp' })).toBe('billing account')
  })
})

describe('supports', () => {
  it('offers Scan now and Reload bills for every provider', () => {
    for (const p of ['aws', 'gcp', 'azure', 'digitalocean'] as const) {
      expect(supports(p, 'scan')).toBe(true)
      expect(supports(p, 'reload')).toBe(true)
    }
  })
})

describe('cloudWords', () => {
  it('reads as before for AWS alone or nothing connected', () => {
    for (const w of [cloudWords([]), cloudWords(['aws'])]) {
      expect(w).toMatchObject({ awsOnly: true, hasAws: true, single: 'AWS', credits: 'AWS credits' })
      expect(w.provisional).toBe('AWS Cost Explorer')
      expect(w.firstBill).toBe('Cost and Usage Report')
    }
  })

  it('names the one provider of a workspace', () => {
    const w = cloudWords(['gcp'])
    expect(w).toMatchObject({ awsOnly: false, hasAws: false, single: 'Google Cloud', credits: 'Google Cloud credits' })
    expect(w.total).toBe("Google Cloud's own total")
    expect(w.firstBill).toBe('bill')
  })

  it('speaks of clouds for several providers', () => {
    const w = cloudWords(['aws', 'digitalocean'])
    expect(w).toMatchObject({ awsOnly: false, hasAws: true, single: null, credits: 'Credits' })
    expect(w.provisional).not.toMatch(/AWS/)
  })
})
