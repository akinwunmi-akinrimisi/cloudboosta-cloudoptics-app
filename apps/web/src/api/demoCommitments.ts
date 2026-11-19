/**
 * Demo Savings Plans, Reserved Instances and the R8 advisory, worked out with the same
 * arithmetic as the API (apps/api/src/cloudoptics/commitments/advisor.py).
 */
import type {
  CommitmentList,
  CommitmentOption,
  CommitmentPayment,
  Recommendation,
  SavingsPlanAdvice,
  SavingsPlanAdvisory,
} from './types'

const HOURS_PER_MONTH = 730
const HOURS_PER_YEAR = 8760
const UPFRONT: Record<CommitmentPayment, number> = { no_upfront: 0, partial_upfront: 0.5, all_upfront: 1 }
const TYPICAL: Record<string, number> = {
  'savings_plan:1:no_upfront': 0.28,
  'savings_plan:1:partial_upfront': 0.31,
  'savings_plan:1:all_upfront': 0.33,
  'savings_plan:3:no_upfront': 0.46,
  'savings_plan:3:partial_upfront': 0.5,
  'savings_plan:3:all_upfront': 0.52,
  'reserved_instance:1:no_upfront': 0.36,
  'reserved_instance:1:partial_upfront': 0.39,
  'reserved_instance:1:all_upfront': 0.4,
  'reserved_instance:3:no_upfront': 0.54,
  'reserved_instance:3:partial_upfront': 0.59,
  'reserved_instance:3:all_upfront': 0.61,
}
// AWS's estimate for the demo payer (1-year Compute Savings Plan).
const AWS_DISCOUNT: Partial<Record<CommitmentPayment, number>> = { no_upfront: 0.3, all_upfront: 0.34 }

const round = (v: number, places = 2) => Math.round(v * 10 ** places) / 10 ** places
const floor3 = (v: number) => Math.floor(Math.round(v * 1e6) / 1e3) / 1e3

function isoDay(offsetDays: number): string {
  return new Date(Date.now() + offsetDays * 86_400_000).toISOString().slice(0, 10)
}

function isoTime(offsetDays: number): string {
  return new Date(Date.now() + offsetDays * 86_400_000).toISOString()
}

function option(
  instrument: CommitmentOption['instrument'],
  termYears: number,
  payment: CommitmentPayment,
  discount: number,
  coveredHourly: number,
  extra: Partial<CommitmentOption> = {},
): CommitmentOption {
  const commitment = extra.hourlyCommitmentUsd ?? floor3(coveredHourly * (1 - discount))
  const covered = commitment / (1 - discount)
  const months = 12 * termYears
  const total = commitment * HOURS_PER_YEAR * termYears
  const upfront = extra.upfrontUsd ?? total * UPFRONT[payment]
  const recurring = Math.max(0, total - upfront) / months
  const onDemand = covered * HOURS_PER_MONTH
  const gain = onDemand - recurring
  return {
    source: 'ours',
    instrument,
    termYears,
    payment,
    lookbackDays: null,
    discountPercent: round(discount * 100, 1),
    hourlyCommitmentUsd: round(commitment, 3),
    coveredOnDemandHourlyUsd: round(covered, 4),
    monthlySavingsUsd: round(onDemand - total / months),
    upfrontUsd: round(upfront),
    monthlyRecurringUsd: round(recurring),
    totalCommitmentUsd: round(total),
    breakEvenMonths: upfront <= 0 ? 0 : round(Math.min(months, gain > 0 ? upfront / gain : months), 1),
    discountFromAws: false,
    ...extra,
  }
}

/** 60 days of Savings-Plan-eligible on-demand spend: busier on weekdays. */
function eligibleDays(): { date: string; amount: number }[] {
  return Array.from({ length: 60 }, (_, i) => {
    const date = isoDay(i - 60)
    const weekday = new Date(`${date}T00:00:00Z`).getUTCDay() % 6 !== 0
    return { date, amount: round((weekday ? 44 : 31) + ((i * 7) % 5)) }
  })
}

function percentile(values: number[], p: number): number {
  const sorted = [...values].sort((a, b) => a - b)
  const rank = ((sorted.length - 1) * p) / 100
  const low = Math.floor(rank)
  const high = Math.ceil(rank)
  const a = sorted[low] ?? 0
  return a + ((sorted[high] ?? a) - a) * (rank - low)
}

export function demoAdvisory(payerAccountId: string, connectionId: string, currency: string, rate: number | null): SavingsPlanAdvisory {
  const daily = eligibleDays()
  // The demo's open idle and rightsizing recommendations on servers.
  const netted = 41.2
  const hourly = daily.map((d) => Math.max(0, d.amount / 24 - netted / HOURS_PER_MONTH))
  const p10 = percentile(hourly, 10)
  const p20 = percentile(hourly, 20)
  const target = 0.6 * p10
  const options: CommitmentOption[] = []
  for (const instrument of ['savings_plan', 'reserved_instance'] as const) {
    for (const term of [1, 3]) {
      for (const payment of ['no_upfront', 'partial_upfront', 'all_upfront'] as const) {
        const aws = instrument === 'savings_plan' && term === 1 ? AWS_DISCOUNT[payment] : undefined
        options.push(
          option(instrument, term, payment, aws ?? TYPICAL[`${instrument}:${term}:${payment}`] ?? 0, target, {
            discountFromAws: aws !== undefined,
          }),
        )
      }
    }
  }
  for (const [lookbackDays, scale] of [[30, 1.12], [60, 1]] as const) {
    for (const payment of ['no_upfront', 'all_upfront'] as const) {
      const discount = AWS_DISCOUNT[payment] ?? 0
      const commitment = floor3(target * 1.6 * scale * (1 - discount))
      options.push(
        option('savings_plan', 1, payment, discount, 0, {
          source: 'aws',
          lookbackDays,
          hourlyCommitmentUsd: commitment,
          upfrontUsd: payment === 'all_upfront' ? round(commitment * HOURS_PER_YEAR) : 0,
        }),
      )
    }
  }
  const def = options[0] as CommitmentOption
  const ladder: SavingsPlanAdvice['ladder'] = []
  let share = 0.25
  let held = 0
  let month = 0
  while (held < def.hourlyCommitmentUsd - 1e-9) {
    const total = share >= 1 - 1e-9 ? def.hourlyCommitmentUsd : floor3(def.hourlyCommitmentUsd * share)
    const buy = round(total - held, 3)
    if (buy >= 0.001) {
      const start = new Date()
      const buyOn =
        month === 0
          ? isoDay(0)
          : new Date(Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + month, 1)).toISOString().slice(0, 10)
      ladder.push({ month, buyOn, buyHourlyUsd: buy, totalHourlyUsd: round(total, 3), sharePercent: Math.round(Math.min(share, 1) * 100) })
      held = total
      month += 1
    }
    share = Math.min(1, share + 0.1)
  }
  const average = hourly.reduce((t, v) => t + v, 0) / hourly.length
  const monthly = def.totalCommitmentUsd / 12
  const advice: SavingsPlanAdvice = {
    connectionId,
    payerAccountId,
    status: 'ok',
    dataThrough: daily[daily.length - 1]?.date ?? null,
    baseline: {
      days: 60,
      nettedUsdMonth: netted,
      averageHourlyUsd: round(average, 4),
      p10HourlyUsd: round(p10, 4),
      p20HourlyUsd: round(p20, 4),
      targetHourlyUsd: round(target, 4),
      bandLowHourlyUsd: round(target, 4),
      bandHighHourlyUsd: round(0.8 * p20, 4),
    },
    dailyEligible: daily,
    options,
    default: def,
    ladder,
    fx:
      currency !== 'USD' && rate
        ? {
            currency,
            rate,
            rateDate: isoDay(0),
            estimated: false,
            scenarios: [0, 20, 40].map((w) => {
              const r = rate * (1 + w / 100)
              return {
                weakeningPercent: w,
                rate: r,
                monthlyLocal: round(monthly * r),
                upfrontLocal: round(def.upfrontUsd * r),
                totalLocal: round(def.totalCommitmentUsd * r),
              }
            }),
          }
        : null,
    fxExposed: currency !== 'USD',
    currentCoveragePercent: 18.4,
    targetCoveragePercent: round(18.4 + (def.coveredOnDemandHourlyUsd / average) * 81.6 * 0.95, 1),
    utilisationPercent: 96.2,
    declineTolerancePercent: round((1 - def.coveredOnDemandHourlyUsd / average) * 100, 1),
    awsFetchedAt: isoTime(-0.3),
    awsErrors: [],
  }
  return { advice: [advice], returnWindowDays: 7, ladderFirstPercent: 25, ladderStepPercent: 10, fxStressPercent: [20, 40] }
}

export function demoCommitments(payerAccountId: string, connectionId: string): CommitmentList {
  const base = {
    connectionId,
    provider: 'aws' as const,
    planType: null,
    paymentOption: null,
    state: 'active',
    quantity: null,
    upfrontUsd: 0,
    current: true,
    lastSeenAt: isoTime(-0.3),
  }
  const ends = (days: number) => ({ endsAt: isoTime(days), daysLeft: days })
  return {
    commitments: [
      {
        ...base,
        id: 'demo-commitment-1',
        kind: 'ec2_reserved',
        commitmentId: '3f1c2b4a-5e6d-4c7b-8a9f-0e1d2c3b4a59',
        accountId: '111111111111',
        regionId: 'eu-west-1',
        description: '2 x m5.large Linux/UNIX',
        planType: 'standard',
        termMonths: 12,
        paymentOption: 'No Upfront',
        quantity: 2,
        hourlyCommitmentUsd: 0.124,
        startsAt: isoTime(-337),
        ...ends(28),
        utilisationPct: 99.1,
      },
      {
        ...base,
        id: 'demo-commitment-2',
        kind: 'savings_plan',
        commitmentId: `arn:aws:savingsplans::${payerAccountId}:savingsplan/7d2e1f30-demo`,
        accountId: payerAccountId,
        regionId: null,
        description: 'Compute Savings Plan',
        planType: 'Compute',
        termMonths: 12,
        paymentOption: 'No Upfront',
        hourlyCommitmentUsd: 0.25,
        startsAt: isoTime(-310),
        ...ends(55),
        utilisationPct: 96.2,
      },
      {
        ...base,
        id: 'demo-commitment-3',
        kind: 'rds_reserved',
        commitmentId: 'demo-postgres-ri',
        accountId: '111111111111',
        regionId: 'af-south-1',
        description: '1 x db.t4g.medium postgresql',
        termMonths: 36,
        paymentOption: 'Partial Upfront',
        quantity: 1,
        hourlyCommitmentUsd: 0.041,
        upfrontUsd: 520,
        startsAt: isoTime(-400),
        ...ends(695),
        utilisationPct: 100,
      },
    ],
    coverage: [
      {
        connectionId,
        savingsPlansCoveragePercent: 18.4,
        savingsPlansUtilisationPercent: 96.2,
        reservationsCoveragePercent: 9.7,
        reservationsUtilisationPercent: 99.4,
        fetchedAt: isoTime(-0.3),
        errors: [],
      },
    ],
    expiryAlertDays: [60, 30, 15],
  }
}

/** The advisory as the R8 recommendation the engine stores. */
export function demoR8(advisory: SavingsPlanAdvisory): Partial<Recommendation> & Pick<Recommendation, 'id' | 'ruleId' | 'type' | 'accountId' | 'regionId' | 'resourceId' | 'title' | 'costNowUsdMonth' | 'costAfterUsdMonth'> {
  const a = advisory.advice[0] as SavingsPlanAdvice
  const o = a.default as CommitmentOption
  const onDemand = round(o.coveredOnDemandHourlyUsd * HOURS_PER_MONTH)
  const first = a.ladder[0]
  return {
    id: 'demo-rec-r8',
    ruleId: 'R8_savings_plan',
    type: 'buy_savings_plan',
    accountId: a.payerAccountId,
    regionId: 'global',
    resourceId: 'compute-savings-plan',
    title: `Commit to a 1-year Compute Savings Plan of USD ${o.hourlyCommitmentUsd.toFixed(3)} an hour (No Upfront), bought in steps`,
    costNowUsdMonth: onDemand,
    costAfterUsdMonth: round(onDemand - o.monthlySavingsUsd),
    reversibility: 'irreversible',
    riskScore: a.fxExposed ? 35 : 20,
    riskFactors: [
      { factor: 'one_year_commitment', points: 20 },
      ...(a.fxExposed ? [{ factor: 'paid_in_dollars', points: 15 }] : []),
    ],
    confidence: 'high',
    confidenceNotes: ['hourly_from_daily_cur', 'advisory_only', 'other_savings_netted'],
    evidence: [
      { kind: 'eligibleOnDemandUsdHour', value: a.baseline.averageHourlyUsd, days: a.baseline.days },
      { kind: 'baselineP10UsdHour', value: a.baseline.p10HourlyUsd, days: a.baseline.days },
      { kind: 'nettedSavingsUsdMonth', value: a.baseline.nettedUsdMonth },
      { kind: 'coveragePercent', value: a.currentCoveragePercent },
      { kind: 'usageDeclineTolerancePercent', value: a.declineTolerancePercent },
    ],
    fix: {
      steps: [],
      rollback: [],
      checklist: [
        `Buy the first step now: USD ${(first?.buyHourlyUsd ?? o.hourlyCommitmentUsd).toFixed(3)} an hour`,
        'Then add about 10% of the target each month while usage holds (see the Commitments page for the ladder).',
        'AWS lets you return a Savings Plan within 7 days of buying it if the hourly commitment is USD 100 or less: check the purchase in that week.',
      ],
      console: {
        url: 'https://console.aws.amazon.com/costmanagement/home#/savings-plans/purchase',
        steps: [
          'Open Savings Plans, Purchase Savings Plans in the AWS console (payer account).',
          'Choose Compute Savings Plans, 1-year term, No Upfront.',
          'Enter the hourly commitment of the first step, add to cart and submit.',
        ],
      },
      notes: 'Advisory only: Cloudoptics never buys anything. A Savings Plan cannot be cancelled after the return window.',
    },
  }
}
