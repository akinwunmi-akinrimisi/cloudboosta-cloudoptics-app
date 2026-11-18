import type { CurrencyMode } from '../lib/currency'
import { read, write } from '../lib/storage'
import type {
  AccountList,
  AlertSettings,
  AlertSettingsView,
  Anomaly,
  AnomalyDetail,
  AnomalyStatus,
  CommitmentList,
  CreditBalanceInput,
  CreditsRunway,
  DigestPreview,
  MonthlyReport,
  ReportList,
  NotificationChoices,
  NotificationPreferences,
  Api,
  AuditEntry,
  Budget,
  BudgetInput,
  BudgetStatus,
  AuditPage,
  CloudAccount,
  AuthConfig,
  Connection,
  ConnectionCreate,
  ConnectionIntent,
  ConnectionSetup,
  CostBreakdown,
  CostChanges,
  CostContributor,
  CostDimension,
  CostForecast,
  CostMonths,
  CostSummary,
  CurrentRate,
  DailyCosts,
  IntentSetup,
  Me,
  InventorySummary,
  Invite,
  LastScan,
  Member,
  MonthState,
  PeriodReload,
  PermissionCheck,
  Recommendation,
  ReloadStatus,
  RecommendationChange,
  RecommendationEvent,
  RecommendationQuery,
  RecommendationStatus,
  RecommendationSummary,
  ResourceScan,
  Role,
  RuleSettings,
  RuleSettingsView,
  SavingsPlanAdvisory,
  Tenant,
  TenantChange,
  UsageSeries,
} from './types'
import { ApiError } from './types'
import { demoAdvisory, demoCommitments, demoR8 } from './demoCommitments'

// In-browser stand-in for the API, used by the public GitHub Pages build. Nothing leaves
// the browser and no AWS call is made; state lives in this viewer's localStorage only.

const KEY = 'co.demo.v1'
// Illustrative rates for the demo only; real rates are set by Cloudoptics staff (FR-FX-06).
const DEMO_RATES: Record<string, number> = {
  NGN: 1530.25,
  KES: 129.4,
  GHS: 15.5,
  ZAR: 18.1,
  EGP: 48.6,
  RWF: 1410,
  UGX: 3690,
  TZS: 2600,
  GBP: 0.79,
  EUR: 0.92,
}
const COUNTRY: Record<string, [string, string]> = {
  NG: ['NGN', 'Africa/Lagos'],
  KE: ['KES', 'Africa/Nairobi'],
  GH: ['GHS', 'Africa/Accra'],
  ZA: ['ZAR', 'Africa/Johannesburg'],
  EG: ['EGP', 'Africa/Cairo'],
  RW: ['RWF', 'Africa/Kigali'],
  UG: ['UGX', 'Africa/Kampala'],
  TZ: ['TZS', 'Africa/Dar_es_Salaam'],
  GB: ['GBP', 'Europe/London'],
}

interface State {
  me: Me | null
  tenants: Tenant[]
  connections: Record<string, Connection[]>
  externalIds: Record<string, string>
  /** tenantId:recommendationId -> status changed in the demo. */
  recommendationStatus: Record<string, RecommendationStatus>
  /** tenantId:recommendationId -> assignee and history in the demo. */
  recommendationAssignee?: Record<string, string | null>
  recommendationEvents?: Record<string, RecommendationEvent[]>
  /** tenantId:recommendationId -> when it was marked done in the demo (verified a few seconds later). */
  recommendationDoneAt?: Record<string, string>
  ruleSettings?: Record<string, RuleSettings>
  /** connectionId -> when the demo last "scanned" it. */
  scannedAt?: Record<string, string>
  /** connectionId -> when the demo's running scan started (it takes `scanMs`). */
  scanStartedAt?: Record<string, string>
  /** Budgets per tenant, as entered; how the month is going is worked out on each read. */
  budgets?: Record<string, (BudgetInput & { id: string; isActive: boolean; createdAt: string })[]>
  /** tenantId:anomalyId -> status and note set in the demo. */
  anomalyStatus?: Record<string, { status: AnomalyStatus; note: string | null; at: string }>
  alertSettings?: Record<string, AlertSettings>
  /** tenantId -> this browser's choices, and tenantId -> workspace defaults. */
  notificationsMine?: Record<string, Partial<NotificationChoices>>
  notificationDefaults?: Record<string, Partial<NotificationChoices>>
  /** tenantId -> months whose report was "made" in the demo, and extra report addresses. */
  reportsMade?: Record<string, Record<string, string>>
  reportEmails?: Record<string, string[]>
  /** tenantId -> credit balances entered in the demo, newest last. */
  creditBalances?: Record<string, (CreditBalanceInput & { enteredAt: string; asOf: string })[]>
  /** Open invites per tenant. Nobody else signs in to the demo, so they stay pending. */
  invites?: Record<string, Invite[]>
  /** Connections whose accounts have the resource stack (after a demo scan). */
  resourceAccess?: Record<string, boolean>
  /** connectionId -> the demo's latest reload of its bills. */
  reloads?: Record<string, { requestedAt: string; period: string | null }>
  /** One-click links waiting for the (simulated) stack to report back. */
  intents?: Record<string, { tenantId: string; createdAt: number; connectionId: string | null }>
}

/** The demo's ended months, newest first: last month matches its monthly report. */
const DEMO_MONTH_TOTALS = [1186.4, 1237.75, 1201.3]
const RELOAD_EVERY_MS = 3_600_000

/** As the API: final from the 6th of the month after. */
function demoMonthState(month: string): MonthState {
  const now = new Date()
  const current = now.toISOString().slice(0, 7)
  if (month >= current) return 'current'
  const next = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 6))
  return now.getTime() >= next.getTime() ? 'final' : 'changing'
}

const empty = (): State => ({
  me: null,
  tenants: [],
  connections: {},
  externalIds: {},
  recommendationStatus: {},
})

const DEFAULT_ALERT_SETTINGS: AlertSettings = {
  anomalyMinImpact: 50,
  anomalyMinImpactCurrency: 'USD',
  anomalyMinPercent: 30,
  anomalyEmails: [],
}

type DemoAnomaly = Pick<
  Anomaly,
  'id' | 'accountId' | 'accountName' | 'serviceCode' | 'serviceName' | 'contributors' | 'status' | 'note'
> & { daysAgo: number; base: number; spike: number }

// Illustrative anomalies for the demo estate: a forgotten load test and a NAT data spike.
const DEMO_ANOMALIES: DemoAnomaly[] = [
  {
    id: 'demo-anomaly-1',
    daysAgo: 2,
    accountId: '333333333333',
    accountName: 'demo-dev',
    serviceCode: 'AmazonEC2',
    serviceName: 'Amazon Elastic Compute Cloud',
    base: 18.4,
    spike: 142.6,
    status: 'open',
    note: null,
    contributors: [
      { usageType: 'EU-BoxUsage:c5.4xlarge', region: 'eu-west-1', actualUsd: 97.92, expectedUsd: 0, impactUsd: 97.92 },
      { usageType: 'EU-EBS:VolumeUsage.gp3', region: 'eu-west-1', actualUsd: 21.6, expectedUsd: 4.1, impactUsd: 17.5 },
      { usageType: 'EU-DataTransfer-Regional-Bytes', region: 'eu-west-1', actualUsd: 9.2, expectedUsd: 0.4, impactUsd: 8.8 },
    ],
  },
  {
    id: 'demo-anomaly-2',
    daysAgo: 9,
    accountId: '111111111111',
    accountName: 'demo-prod',
    serviceCode: 'AmazonVPC',
    serviceName: 'Amazon Virtual Private Cloud',
    base: 6.2,
    spike: 71.5,
    status: 'investigating',
    note: 'Backup job copying to S3 through the NAT gateway?',
    contributors: [
      { usageType: 'AFS1-NatGateway-Bytes', region: 'af-south-1', actualUsd: 63.1, expectedUsd: 1.9, impactUsd: 61.2 },
      { usageType: 'AFS1-NatGateway-Hours', region: 'af-south-1', actualUsd: 4.4, expectedUsd: 4.3, impactUsd: 0.1 },
    ],
  },
]

const isoDaysAgo = (days: number): string => {
  const now = new Date()
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - days * 86_400_000)
    .toISOString()
    .slice(0, 10)
}

const OPEN: RecommendationStatus[] = ['new', 'assigned', 'in_progress']

type DemoRec = Pick<
  Recommendation,
  'id' | 'ruleId' | 'type' | 'accountId' | 'regionId' | 'resourceId' | 'title' | 'costNowUsdMonth' | 'costAfterUsdMonth'
> &
  Partial<Recommendation>

// Illustrative findings for the demo estate (same accounts and regions as the cost data).
const DEMO_RECS: DemoRec[] = [
  {
    id: 'demo-rec-1',
    ruleId: 'R1_unattached_ebs',
    type: 'delete_unattached_volume',
    accountId: '333333333333',
    regionId: 'eu-west-1',
    resourceId: 'vol-0a1b2c3d4e5f60718',
    title: 'Delete unattached gp2 volume build-cache (500 GiB)',
    costNowUsdMonth: 55,
    costAfterUsdMonth: 0,
    reversibility: 'with_snapshot',
    riskScore: 30,
    riskFactors: [{ factor: 'no_recent_snapshot', points: 30 }],
    evidence: [{ kind: 'state', value: 'available', sinceDays: 41 }],
    fix: {
      steps: [
        "aws ec2 create-snapshot --volume-id vol-0a1b2c3d4e5f60718 --description 'CloudOptics backup of vol-0a1b2c3d4e5f60718' --region eu-west-1",
        'aws ec2 wait snapshot-completed --filters Name=volume-id,Values=vol-0a1b2c3d4e5f60718 --region eu-west-1',
        'aws ec2 delete-volume --volume-id vol-0a1b2c3d4e5f60718 --region eu-west-1',
      ],
      rollback: [
        'aws ec2 create-volume --snapshot-id <snapshot-id> --availability-zone eu-west-1a --volume-type gp2 --region eu-west-1',
      ],
      notes: 'Keep the snapshot until you are sure the data is not needed.',
      console: {
        url: 'https://eu-west-1.console.aws.amazon.com/ec2/home?region=eu-west-1#VolumeDetails:volumeId=vol-0a1b2c3d4e5f60718',
        steps: [
          'Open the volume in the AWS console.',
          'Choose Actions, then Create snapshot, and wait until the snapshot shows Completed.',
          'Choose Actions, then Delete volume, and confirm.',
        ],
      },
    },
  },
  {
    id: 'demo-rec-2',
    ruleId: 'R3_gp2_gp3',
    type: 'modify_volume_gp3',
    accountId: '111111111111',
    regionId: 'eu-west-1',
    resourceId: 'vol-0f9e8d7c6b5a40312',
    title: 'Change volume db-data from gp2 to gp3 (1000 GiB)',
    costNowUsdMonth: 110,
    costAfterUsdMonth: 98.8,
    reversibility: 'reversible',
    riskScore: 25,
    riskFactors: [{ factor: 'production', points: 25 }],
    recommendedConfig: { volumeType: 'gp3', iops: 3000, throughput: 250 },
    evidence: [{ kind: 'volumeType', value: 'gp2', sizeGiB: 1000 }],
    fix: {
      steps: [
        'aws ec2 modify-volume --volume-id vol-0f9e8d7c6b5a40312 --volume-type gp3 --iops 3000 --throughput 250 --region eu-west-1',
      ],
      rollback: ['aws ec2 modify-volume --volume-id vol-0f9e8d7c6b5a40312 --volume-type gp2 --region eu-west-1'],
      notes: 'Online change, no restart. A volume can be modified once every 6 hours.',
    },
  },
  {
    id: 'demo-rec-3',
    ruleId: 'R2_idle_lb',
    type: 'delete_idle_load_balancer',
    accountId: '333333333333',
    regionId: 'af-south-1',
    resourceId: 'staging-old-alb',
    title: 'Delete idle application load balancer staging-old-alb (no healthy targets)',
    costNowUsdMonth: 21.39,
    costAfterUsdMonth: 0,
    riskScore: 0,
    riskFactors: [],
    confidenceNotes: ['hourly_charge_only_lcu_excluded'],
    evidence: [{ kind: 'idleDays', value: 19 }],
    fix: {
      steps: [
        'aws elbv2 delete-load-balancer --load-balancer-arn arn:aws:elasticloadbalancing:af-south-1:333333333333:loadbalancer/app/staging-old-alb/50dc6c495c0c9188 --region af-south-1',
      ],
      rollback: [],
      notes: 'Check DNS records and listeners pointing at it before deleting.',
    },
  },
  {
    id: 'demo-rec-4',
    ruleId: 'R1_old_snapshot',
    type: 'delete_old_snapshot',
    accountId: '111111111111',
    regionId: 'eu-west-1',
    resourceId: 'snap-07c1d2e3f4a5b6c7d',
    title: 'Delete 412-day-old snapshot snap-07c1d2e3f4a5b6c7d of a deleted volume',
    costNowUsdMonth: 10,
    costAfterUsdMonth: 0,
    riskScore: 55,
    riskFactors: [
      { factor: 'production', points: 25 },
      { factor: 'data_loss', points: 30 },
    ],
    confidence: 'medium',
    confidenceNotes: ['savings_upper_bound_incremental'],
    evidence: [{ kind: 'sourceVolume', value: 'vol-0123456789abcdef0', exists: false }],
    fix: {
      steps: ['aws ec2 delete-snapshot --snapshot-id snap-07c1d2e3f4a5b6c7d --region eu-west-1'],
      rollback: [],
      notes: 'Deleting a snapshot cannot be undone.',
      alternative: {
        title: 'Archive instead (about 75% cheaper, 90-day minimum)',
        monthlyCostUsd: 2.5,
        steps: [
          'aws ec2 modify-snapshot-tier --snapshot-id snap-07c1d2e3f4a5b6c7d --storage-tier archive --region eu-west-1',
        ],
      },
    },
  },
  {
    id: 'demo-rec-5',
    ruleId: 'R2_idle_eip',
    type: 'release_idle_ip',
    accountId: '333333333333',
    regionId: 'af-south-1',
    resourceId: 'eipalloc-0a1b2c3d4e5f6a7b8',
    title: 'Release idle Elastic IP 203.0.113.25 (not attached to anything)',
    costNowUsdMonth: 3.65,
    costAfterUsdMonth: 0,
    riskScore: 0,
    riskFactors: [],
    evidence: [{ kind: 'state', value: 'unassociated', sinceDays: 12 }],
    fix: {
      steps: ['aws ec2 release-address --allocation-id eipalloc-0a1b2c3d4e5f6a7b8 --region af-south-1'],
      rollback: [],
      notes: 'A released address cannot be got back; update DNS or allow-lists that use it first.',
    },
  },
  {
    id: 'demo-rec-8',
    ruleId: 'R10_idle_nat',
    type: 'delete_idle_nat_gateway',
    accountId: '333333333333',
    regionId: 'af-south-1',
    resourceId: 'nat-0d1e2f3a4b5c6d7e8',
    title: 'Delete idle NAT gateway nat-0d1e2f3a4b5c6d7e8 (no traffic in 30 days)',
    costNowUsdMonth: 43.8,
    costAfterUsdMonth: 0,
    reversibility: 'reversible',
    riskScore: 15,
    riskFactors: [{ factor: 'routes_unchecked', points: 15 }],
    confidenceNotes: ['route_tables_not_checked'],
    currentConfig: { hourlyUsd: 0.06, bytesProcessed: 0 },
    evidence: [
      { kind: 'natBytes', value: 0, days: 30 },
      { kind: 'natHours', value: 720, days: 30 },
      { kind: 'billedDays', value: 30, days: 30 },
      { kind: 'usageType', value: 'AFS1-NatGateway-Hours' },
    ],
    fix: {
      steps: [
        'aws ec2 describe-nat-gateways --nat-gateway-ids nat-0d1e2f3a4b5c6d7e8 --region af-south-1 > nat-0d1e2f3a4b5c6d7e8.json',
        "aws ec2 describe-route-tables --filters Name=route.nat-gateway-id,Values=nat-0d1e2f3a4b5c6d7e8 --query 'RouteTables[].[RouteTableId,Routes[?NatGatewayId==`nat-0d1e2f3a4b5c6d7e8`].DestinationCidrBlock]' --region af-south-1",
        "aws ec2 delete-route --route-table-id '<route-table-id>' --destination-cidr-block 0.0.0.0/0 --region af-south-1",
        'aws ec2 delete-nat-gateway --nat-gateway-id nat-0d1e2f3a4b5c6d7e8 --region af-south-1',
      ],
      rollback: [
        "aws ec2 create-nat-gateway --subnet-id '<subnet-id>' --allocation-id '<allocation-id>' --region af-south-1",
        "aws ec2 create-route --route-table-id '<route-table-id>' --destination-cidr-block 0.0.0.0/0 --nat-gateway-id '<new-nat-gateway-id>' --region af-south-1",
      ],
      notes:
        "The first command saves the gateway's configuration to nat-0d1e2f3a4b5c6d7e8.json; take the subnet and Elastic IP allocation from it to recreate the gateway. Wait a few days between removing the routes and deleting the gateway. Keep the Elastic IP if allow-lists use its address.",
      console: {
        url: 'https://af-south-1.console.aws.amazon.com/vpc/home?region=af-south-1#NatGatewayDetails:natGatewayId=nat-0d1e2f3a4b5c6d7e8',
        steps: [
          'Open the NAT gateway in the AWS console and note its subnet, Elastic IP allocation ID and tags.',
          "Open Route tables, search for the gateway's ID and delete each route that points at it.",
          'Wait a few days and check that nothing broke (no failed outbound calls).',
          'Back on the NAT gateway, choose Actions, then Delete NAT gateway, and confirm. Keep its Elastic IP, or release it if you will not recreate the gateway.',
        ],
      },
    },
  },
  {
    id: 'demo-rec-9',
    ruleId: 'R6b_extended_support',
    type: 'upgrade_extended_support',
    accountId: '111111111111',
    regionId: 'eu-west-1',
    resourceId: 'reports-db',
    title: 'Upgrade database reports-db off PostgreSQL 12 Extended Support',
    costNowUsdMonth: 288,
    costAfterUsdMonth: 0,
    effort: 'M',
    reversibility: 'with_snapshot',
    restartRequired: true,
    riskScore: 60,
    riskFactors: [
      { factor: 'production', points: 25 },
      { factor: 'version_upgrade', points: 35 },
    ],
    currentConfig: { service: 'rds', engine: 'postgres', version: '12', supportYears: 'Yr1-Yr2' },
    recommendedConfig: { action: 'upgrade', engineVersion: 'standard_support' },
    evidence: [
      { kind: 'extendedSupportUsd', value: 288, days: 30 },
      { kind: 'billedDays', value: 30, days: 30 },
      { kind: 'usageType', value: 'EU-ExtendedSupport:Yr1-Yr2:PostgreSQL12' },
    ],
    fix: {
      steps: [
        "aws rds describe-db-engine-versions --engine postgres --engine-version 12 --query 'DBEngineVersions[].ValidUpgradeTarget[].EngineVersion' --region eu-west-1",
        'aws rds create-db-snapshot --db-instance-identifier reports-db --db-snapshot-identifier reports-db-before-upgrade --region eu-west-1',
        'aws rds wait db-snapshot-available --db-snapshot-identifier reports-db-before-upgrade --region eu-west-1',
        "aws rds modify-db-instance --db-instance-identifier reports-db --engine-version '<target-version>' --allow-major-version-upgrade --apply-immediately --region eu-west-1",
      ],
      rollback: [
        'aws rds restore-db-instance-from-db-snapshot --db-instance-identifier reports-db-restored --db-snapshot-identifier reports-db-before-upgrade --region eu-west-1',
      ],
      notes:
        "Pick a target from the first command's list that is in standard support. Major upgrades take the database offline for a few minutes and cannot be undone in place: to go back, restore the snapshot to a new database and point the application at it. Upgrade any read replicas and check the parameter group for the new version.",
      console: {
        url: 'https://eu-west-1.console.aws.amazon.com/rds/home?region=eu-west-1#database:id=reports-db;is-cluster=false',
        steps: [
          'Open the database in the AWS console.',
          "Test the upgrade first: restore a recent snapshot to a new database, upgrade that copy and run your application's tests against it.",
          'Choose Actions, then Take snapshot, and wait until the snapshot shows Available.',
          'Choose Modify, set the engine version to one in standard support, choose Continue, then pick when to apply it and choose Modify.',
        ],
      },
    },
  },
]

const SCHEDULER_TEMPLATE =
  'https://raw.githubusercontent.com/akinwunmi-akinrimisi/cloudboosta-cloudoptics-app/main/infra/cloudformation/scheduler/cloudoptics-scheduler.yaml'

/** R7 (PRD FR-ANA-07): non-production resources running 24/7, scheduled in the workspace's time zone. */
function demoScheduleRecs(timeZone: string, settings: RuleSettings): DemoRec[] {
  const { scheduleStart: start, scheduleEnd: end } = settings
  const minutes = (hhmm: string) => Number(hhmm.slice(0, 2)) * 60 + Number(hhmm.slice(3, 5))
  const onWeek = (5 * (minutes(end) - minutes(start))) / 60
  const offMonth = ((168 - onWeek) * 730) / 168
  const city = timeZone.split('/').pop()?.replace(/_/g, ' ') ?? timeZone
  const window = `${start}-${end} weekdays, ${city} time`
  const schedule = (rds: boolean) => ({
    tagKey: 'cloudoptics:schedule',
    tagValue: [`${start}-${end}`, 'MO-FR', timeZone].join(rds ? ' ' : ';'),
    start,
    end,
    days: 'MO-FR',
    timeZone,
    hoursOnPerWeek: onWeek,
    stackName: 'cloudoptics-scheduler',
    templateUrl: SCHEDULER_TEMPLATE,
    quickCreateUrl: null,
    rdsAutoStart: rds,
  })
  const evidence = (by: string, hourly: number) => [
    { kind: 'nonProduction', value: by },
    { kind: 'runningHoursPercent', value: 100, days: 14 },
    { kind: 'scheduledHoursPerWeek', value: onWeek },
    { kind: 'hoursOffPerMonth', value: Math.round(offMonth * 10) / 10 },
    { kind: 'hourlyRateUsd', value: hourly },
  ]
  const deploy =
    'Once per account and region: deploy the cloudoptics-scheduler stack from the link above. It runs in your account; Cloudoptics stays read-only.'
  const server = schedule(false)
  const db = schedule(true)
  const ec2Hourly = 0.0528
  const rdsHourly = 0.088
  return [
    {
      id: 'demo-rec-6',
      ruleId: 'R7_schedule_ec2',
      type: 'schedule_instance',
      accountId: '333333333333',
      regionId: 'af-south-1',
      resourceId: 'i-0c4d5e6f7a8b9c0d1',
      title: `Run non-production server staging-api (t3.medium) only in office hours: ${window}`,
      costNowUsdMonth: Math.round(ec2Hourly * 730 * 100) / 100,
      costAfterUsdMonth: Math.round(ec2Hourly * (730 - offMonth) * 100) / 100,
      savingsBasis: 'list_price',
      effort: 'M',
      reversibility: 'reversible',
      restartRequired: true,
      riskScore: 15,
      riskFactors: [{ factor: 'stops_out_of_hours', points: 15 }],
      confidenceNotes: ['schedule_saves_compute_only'],
      recommendedConfig: { tagKey: server.tagKey, schedule: server.tagValue },
      evidence: evidence('tag Environment=staging', ec2Hourly),
      fix: {
        steps: [
          `aws ec2 create-tags --resources i-0c4d5e6f7a8b9c0d1 --tags '[{"Key": "cloudoptics:schedule", "Value": "${server.tagValue}"}]' --region af-south-1`,
        ],
        rollback: [
          'aws ec2 delete-tags --resources i-0c4d5e6f7a8b9c0d1 --tags Key=cloudoptics:schedule --region af-south-1',
          'aws ec2 start-instances --instance-ids i-0c4d5e6f7a8b9c0d1 --region af-south-1',
        ],
        schedule: server,
        notes:
          'The scheduler stops the server at the end of the day and starts it in the morning; its disks stay, so nothing on them is lost.',
        console: {
          url: 'https://af-south-1.console.aws.amazon.com/ec2/home?region=af-south-1#InstanceDetails:instanceId=i-0c4d5e6f7a8b9c0d1',
          steps: [
            deploy,
            'Open the server in the AWS console.',
            'Choose the Tags tab, then Manage tags, then Add new tag.',
            `Enter the key cloudoptics:schedule and the value ${server.tagValue}, then choose Save.`,
          ],
        },
      },
    },
    {
      id: 'demo-rec-7',
      ruleId: 'R7_schedule_rds',
      type: 'schedule_database',
      accountId: '333333333333',
      regionId: 'af-south-1',
      resourceId: 'qa-orders-db',
      title: `Run non-production database qa-orders-db (db.t3.medium) only in office hours: ${window}`,
      costNowUsdMonth: Math.round(rdsHourly * 730 * 100) / 100,
      costAfterUsdMonth: Math.round(rdsHourly * (730 - offMonth) * 100) / 100,
      savingsBasis: 'list_price',
      effort: 'M',
      reversibility: 'reversible',
      restartRequired: true,
      riskScore: 45,
      riskFactors: [
        { factor: 'stops_out_of_hours', points: 15 },
        { factor: 'environment_from_name', points: 20 },
        { factor: 'database_stops_daily', points: 10 },
      ],
      confidence: 'medium',
      confidenceNotes: ['storage_not_included', 'rds_restarts_after_7_days'],
      recommendedConfig: { tagKey: db.tagKey, schedule: db.tagValue },
      evidence: evidence('name qa-orders-db', rdsHourly),
      fix: {
        steps: [
          `aws rds add-tags-to-resource --resource-name arn:aws:rds:af-south-1:333333333333:db:qa-orders-db --tags '[{"Key": "cloudoptics:schedule", "Value": "${db.tagValue}"}]' --region af-south-1`,
        ],
        rollback: [
          'aws rds remove-tags-from-resource --resource-name arn:aws:rds:af-south-1:333333333333:db:qa-orders-db --tag-keys cloudoptics:schedule --region af-south-1',
          'aws rds start-db-instance --db-instance-identifier qa-orders-db --region af-south-1',
        ],
        schedule: db,
        notes:
          'A stopped database keeps its data and settings but cannot be reached until it starts again; its storage and backups are still charged.',
        console: {
          url: 'https://af-south-1.console.aws.amazon.com/rds/home?region=af-south-1#database:id=qa-orders-db;is-cluster=false',
          steps: [
            deploy,
            'Open the database in the AWS console.',
            'Choose the Tags tab, then Add tags.',
            `Enter the key cloudoptics:schedule and the value ${db.tagValue}, then choose Add.`,
          ],
        },
      },
    },
  ]
}

const DAY_MS = 86_400_000

// Already verified in the demo (PRD US-5.2): one measured from the bills, one still estimated.
const DEMO_VERIFIED: DemoRec[] = [
  {
    id: 'demo-rec-10',
    ruleId: 'R3_gp2_gp3',
    type: 'modify_volume_gp3',
    accountId: '222222222222',
    regionId: 'eu-west-1',
    resourceId: 'vol-0c4d5e6f7a8b9c0d1',
    title: 'Change volume analytics-scratch from gp2 to gp3 (800 GiB)',
    costNowUsdMonth: 88,
    costAfterUsdMonth: 70.4,
    reversibility: 'reversible',
    riskScore: 15,
    riskFactors: [{ factor: 'environment_unknown', points: 15 }],
    recommendedConfig: { volumeType: 'gp3', iops: 3000, throughput: 125 },
    evidence: [{ kind: 'volumeType', value: 'gp2', sizeGiB: 800 }],
    status: 'verified',
    doneAt: new Date(Date.now() - 24 * DAY_MS).toISOString(),
    changedAt: new Date(Date.now() - 24 * DAY_MS).toISOString(),
    verifiedAt: new Date(Date.now() - 24 * DAY_MS + 9 * 3_600_000).toISOString(),
    verification: {
      check: 'volume_type_gp3',
      outcome: 'implemented',
      checkedAt: new Date(Date.now() - 24 * DAY_MS + 9 * 3_600_000).toISOString(),
      estimateUsdMonth: 17.6,
      measurement: {
        beforeDays: 30,
        afterDays: 22,
        beforeUsdDay: 2.93,
        afterUsdDay: 2.35,
        measuredAt: new Date(Date.now() - DAY_MS).toISOString(),
      },
    },
    realisedSavingsUsdMonth: 17.4,
    realisedBasis: 'cur',
    realisedAt: new Date(Date.now() - 16 * DAY_MS).toISOString(),
    fix: {
      steps: [
        'aws ec2 modify-volume --volume-id vol-0c4d5e6f7a8b9c0d1 --volume-type gp3 --iops 3000 --throughput 125 --region eu-west-1',
      ],
      rollback: ['aws ec2 modify-volume --volume-id vol-0c4d5e6f7a8b9c0d1 --volume-type gp2 --region eu-west-1'],
    },
  },
  {
    id: 'demo-rec-11',
    ruleId: 'R2_idle_eip',
    type: 'release_idle_ip',
    accountId: '111111111111',
    regionId: 'eu-west-1',
    resourceId: 'eipalloc-0f1e2d3c4b5a69788',
    title: 'Release idle Elastic IP 198.51.100.7 (not attached to anything)',
    costNowUsdMonth: 3.65,
    costAfterUsdMonth: 0,
    evidence: [{ kind: 'state', value: 'unassociated', sinceDays: 30 }],
    status: 'verified',
    changedAt: new Date(Date.now() - 3 * DAY_MS).toISOString(),
    verifiedAt: new Date(Date.now() - 3 * DAY_MS + 5 * 3_600_000).toISOString(),
    verification: {
      check: 'address_released',
      outcome: 'implemented',
      checkedAt: new Date(Date.now() - 3 * DAY_MS + 5 * 3_600_000).toISOString(),
      estimateUsdMonth: 3.65,
    },
    realisedSavingsUsdMonth: null,
    realisedBasis: null,
    fix: { steps: ['aws ec2 release-address --allocation-id eipalloc-0f1e2d3c4b5a69788 --region eu-west-1'], rollback: [] },
  },
]

/** What the demo's verification "sees" for each rule once a recommendation is marked done. */
const DEMO_CHECKS: Record<string, string> = {
  R1_unattached_ebs: 'volume_deleted',
  R1_old_snapshot: 'snapshot_deleted',
  R2_idle_eip: 'address_released',
  R2_idle_lb: 'load_balancer_deleted',
  R3_gp2_gp3: 'volume_type_gp3',
}

/** Marked done in the demo: "verified" after this long, as if a scan had run. */
const DEMO_VERIFY_MS = 4000

function demoRecommendation(r: DemoRec, status: RecommendationStatus): Recommendation {
  const savings = Math.round((r.costNowUsdMonth - r.costAfterUsdMonth) * 100) / 100
  const riskScore = r.riskScore ?? 0
  const riskLevel = riskScore > 65 ? 'high' : riskScore >= 35 ? 'medium' : 'low'
  const confidence = r.confidence ?? 'high'
  return {
    currentConfig: {},
    recommendedConfig: { action: 'delete' },
    savingsUsdMonth: savings,
    savingsBasis: 'effective_rate',
    effort: 'S',
    reversibility: 'irreversible',
    restartRequired: false,
    riskFactors: [],
    confidenceNotes: [],
    evidence: [],
    primaryOf: null,
    firstDetectedAt: new Date(Date.now() - 6 * 86_400_000).toISOString(),
    lastEvaluatedAt: new Date(Date.now() - 3 * 3_600_000).toISOString(),
    ...r,
    riskScore,
    riskLevel,
    confidence,
    priorityScore:
      savings * { high: 1, medium: 0.7, low: 0.4 }[confidence] * { low: 1, medium: 0.6, high: 0.3 }[riskLevel],
    fix: r.fix ?? { steps: [], rollback: [] },
    status,
    assigneeId: null,
  }
}

const DEMO_RULES = [
  'R1_unattached_ebs',
  'R1_old_snapshot',
  'R2_idle_eip',
  'R2_idle_lb',
  'R3_gp2_gp3',
  'R4_idle_ec2',
  'R4_idle_rds',
  'R5_rightsize_ec2',
  'R6_newer_generation',
  'R6_graviton',
  'R7_schedule_ec2',
  'R7_schedule_rds',
  'R6b_extended_support',
  'R10_idle_nat',
  'R8_savings_plan',
]

const DEFAULT_RULE_SETTINGS: RuleSettings = {
  disabledRules: [],
  ignoreTag: 'cloudoptics:ignore',
  idleCpuPercent: 5,
  idleDays: 14,
  nonprodPattern: '(^|[^a-z0-9])(dev|development|test|testing|staging|stage|qa|sandbox|uat)([^a-z]|$)',
  nonprodTagKeys: ['env', 'environment', 'stage'],
  scheduleStart: '07:00',
  scheduleEnd: '20:00',
}

function load(): State {
  const raw = read('local', KEY)
  if (!raw) return empty()
  try {
    return { ...empty(), ...(JSON.parse(raw) as State) }
  } catch {
    return empty()
  }
}

const delay = (ms: number) => new Promise((r) => setTimeout(r, ms))

function randomId(): string {
  return crypto.randomUUID()
}

function externalId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32))
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
}

export class DemoApi implements Api {
  readonly mode = 'demo' as const
  private state: State = load()
  private readonly latencyMs: number

  /** How long a demo resource scan runs, so the progress bar can be seen. */
  private readonly scanMs: number

  /** How long a demo reload of the bills runs (it shows as running until then). */
  private readonly reloadMs: number

  constructor(latencyMs = 250) {
    this.latencyMs = latencyMs
    this.scanMs = latencyMs * 32
    this.reloadMs = latencyMs * 16
  }

  private save(): void {
    write('local', KEY, JSON.stringify(this.state))
  }

  private async tick(): Promise<void> {
    if (this.latencyMs) await delay(this.latencyMs)
  }

  private requireMe(): Me {
    if (!this.state.me) throw new ApiError(401, 'Authentication required')
    return this.state.me
  }

  private tenant(tenantId: string): Tenant {
    const t = this.state.tenants.find((x) => x.id === tenantId)
    if (!t) throw new ApiError(404, 'Workspace not found')
    return t
  }

  async authConfig(): Promise<AuthConfig> {
    return {
      mode: 'dev',
      configured: true,
      provider: null,
      issuer: null,
      clientId: null,
    }
  }

  isSignedIn(): boolean {
    return this.state.me !== null
  }

  async signIn(email: string, name?: string): Promise<void> {
    await this.tick()
    const existing = this.state.me
    this.state.me =
      existing && existing.email === email.toLowerCase()
        ? existing
        : {
            id: randomId(),
            email: email.toLowerCase(),
            displayName: name ?? null,
            preferredCurrencyView: 'both',
            memberships: [],
          }
    if (existing && existing.email !== email.toLowerCase()) {
      this.state = { ...empty(), me: this.state.me }
    }
    this.save()
  }

  signOut(): void {
    this.state = empty()
    this.save()
  }

  async me(): Promise<Me> {
    await this.tick()
    return structuredClone(this.requireMe())
  }

  async updateMe(view: CurrencyMode): Promise<Me> {
    const me = this.requireMe()
    me.preferredCurrencyView = view
    this.save()
    return structuredClone(me)
  }

  async createTenant(body: { name: string; countryCode: string }): Promise<Tenant> {
    await this.tick()
    const me = this.requireMe()
    const name = body.name.trim()
    if (!name) throw new ApiError(422, 'Request validation failed', [{ field: 'name', message: 'Enter a name' }])
    const [currency, tz] = COUNTRY[body.countryCode] ?? ['USD', 'UTC']
    const tenant: Tenant = {
      id: randomId(),
      name,
      countryCode: body.countryCode,
      displayCurrency: currency,
      timeZone: tz,
      mfaRequired: false,
      status: 'active',
      createdAt: new Date().toISOString(),
      role: 'admin',
    }
    this.state.tenants.push(tenant)
    me.memberships.push({
      tenantId: tenant.id,
      tenantName: name,
      role: 'admin',
    })
    this.save()
    return structuredClone(tenant)
  }

  async getTenant(tenantId: string): Promise<Tenant> {
    await this.tick()
    return structuredClone(this.tenant(tenantId))
  }

  async updateTenant(tenantId: string, change: TenantChange): Promise<Tenant> {
    await this.tick()
    Object.assign(this.tenant(tenantId), change)
    this.save()
    return structuredClone(this.tenant(tenantId))
  }

  async listMembers(tenantId: string): Promise<Member[]> {
    const tenant = this.tenant(tenantId)
    const me = this.requireMe()
    return [{ userId: me.id, email: me.email, displayName: me.displayName, role: 'admin', joinedAt: tenant.createdAt }]
  }

  /** A budget with this month's spend from the demo bills (same figures as the Overview). */
  private async budgetView(tenantId: string, b: BudgetInput & { id: string; isActive: boolean; createdAt: string }): Promise<Budget> {
    const summary = await this.costSummary(tenantId)
    const total = summary.monthToDate?.effective ?? 0
    let actualUsd = total
    if (b.scope.type === 'accounts') {
      const byAccount = await this.costBreakdown(tenantId, 'account', 50)
      const part = byAccount.items.filter((i) => b.scope.accounts.includes(i.key)).reduce((s, i) => s + i.amount, 0)
      actualUsd = byAccount.total ? (total * part) / byAccount.total : 0
    }
    const forecastUsd = summary.forecast && total ? (summary.forecast.amount * actualUsd) / total : null
    const rate = b.currency === 'USD' ? 1 : ((await this.fxCurrent(tenantId)).quote?.rate ?? null)
    const round = (n: number) => Math.round(n * 100) / 100
    const amountUsd = rate ? round(b.amount / rate) : null
    const hasData = summary.status === 'ready' || summary.status === 'provisional'
    const status: BudgetStatus =
      amountUsd === null ? 'no_rate' : !hasData ? 'no_data' : actualUsd >= amountUsd ? 'over' : (forecastUsd ?? 0) >= amountUsd ? 'at_risk' : 'on_track'
    const pct = (usd: number | null) => (usd === null || !amountUsd ? null : Math.round((1000 * usd) / amountUsd) / 10)
    return {
      ...structuredClone(b),
      status,
      period: `${new Date().toISOString().slice(0, 7)}-01`,
      dataThrough: summary.dataThrough,
      rate,
      amountUsd,
      actual: rate ? round(actualUsd * rate) : null,
      actualUsd: round(actualUsd),
      forecast: rate && forecastUsd !== null ? round(forecastUsd * rate) : null,
      forecastUsd: forecastUsd === null ? null : round(forecastUsd),
      actualPercent: pct(actualUsd),
      forecastPercent: pct(forecastUsd),
      thresholds: b.thresholds.map((t) => {
        const value = t.kind === 'actual' ? actualUsd : forecastUsd
        const crossed = amountUsd !== null && hasData && value !== null && value >= (amountUsd * t.percent) / 100
        return { ...t, crossed, alerted: crossed }
      }),
    }
  }

  async listBudgets(tenantId: string): Promise<Budget[]> {
    await this.tick()
    this.tenant(tenantId)
    return Promise.all((this.state.budgets?.[tenantId] ?? []).map((b) => this.budgetView(tenantId, b)))
  }

  private checkBudget(tenantId: string, body: Partial<BudgetInput>): void {
    const local = this.tenant(tenantId).displayCurrency
    if (body.currency && body.currency !== 'USD' && body.currency !== local) {
      throw new ApiError(422, `A budget is in USD or the workspace currency (${local}).`)
    }
    if (body.amount !== undefined && !(body.amount > 0)) throw new ApiError(422, 'The amount must be more than 0.')
    if (body.name !== undefined && !body.name.trim()) throw new ApiError(422, 'A budget needs a name.')
    if (body.scope?.type === 'accounts' && body.scope.accounts.length === 0) {
      throw new ApiError(422, 'Choose at least one account.')
    }
  }

  async createBudget(tenantId: string, body: BudgetInput): Promise<Budget> {
    await this.tick()
    this.checkBudget(tenantId, body)
    const stored = { ...structuredClone(body), name: body.name.trim(), id: randomId(), isActive: true, createdAt: new Date().toISOString() }
    ;((this.state.budgets ??= {})[tenantId] ??= []).push(stored)
    this.save()
    return this.budgetView(tenantId, stored)
  }

  async updateBudget(tenantId: string, id: string, change: Partial<BudgetInput> & { isActive?: boolean }): Promise<Budget> {
    await this.tick()
    this.checkBudget(tenantId, change)
    const stored = this.state.budgets?.[tenantId]?.find((b) => b.id === id)
    if (!stored) throw new ApiError(404, 'Budget not found')
    Object.assign(stored, structuredClone(change), change.name ? { name: change.name.trim() } : {})
    this.save()
    return this.budgetView(tenantId, stored)
  }

  async deleteBudget(tenantId: string, id: string): Promise<void> {
    await this.tick()
    const list = this.state.budgets?.[tenantId] ?? []
    if (!list.some((b) => b.id === id)) throw new ApiError(404, 'Budget not found')
    this.state.budgets![tenantId] = list.filter((b) => b.id !== id)
    this.save()
  }

  async inviteMember(tenantId: string, email: string, role: Role): Promise<Invite> {
    await this.tick()
    this.tenant(tenantId)
    const me = this.requireMe()
    const address = email.trim().toLowerCase()
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(address)) {
      throw new ApiError(422, 'Request validation failed', [{ field: 'email', message: 'Enter an email address' }])
    }
    if (address === me.email) throw new ApiError(409, `${address} is already in this workspace`)
    const now = new Date()
    const invite: Invite = {
      id: randomId(),
      email: address,
      role,
      invitedByEmail: me.email,
      createdAt: now.toISOString(),
      expiresAt: new Date(now.getTime() + 7 * 86_400_000).toISOString(),
      status: 'pending',
      emailSent: false,
    }
    const invites = ((this.state.invites ??= {})[tenantId] ??= [])
    this.state.invites[tenantId] = [invite, ...invites.filter((i) => i.email !== address)]
    this.save()
    return structuredClone(invite)
  }

  async listInvites(tenantId: string): Promise<Invite[]> {
    await this.tick()
    this.tenant(tenantId)
    return structuredClone(this.state.invites?.[tenantId] ?? []).map((i) => ({ ...i, emailSent: null }))
  }

  async revokeInvite(tenantId: string, inviteId: string): Promise<void> {
    await this.tick()
    const invites = this.state.invites?.[tenantId] ?? []
    if (!invites.some((i) => i.id === inviteId)) throw new ApiError(404, 'Invite not found')
    ;(this.state.invites ??= {})[tenantId] = invites.filter((i) => i.id !== inviteId)
    this.save()
  }

  // The demo workspace has one member, its only admin.
  async changeRole(tenantId: string, userId: string, role: Role): Promise<void> {
    await this.tick()
    this.tenant(tenantId)
    if (userId === this.requireMe().id && role !== 'admin') {
      throw new ApiError(409, 'A workspace needs at least one admin. Make someone else admin first.')
    }
  }

  async removeMember(tenantId: string, userId: string): Promise<void> {
    await this.tick()
    this.tenant(tenantId)
    if (userId === this.requireMe().id) {
      throw new ApiError(409, 'A workspace needs at least one admin. Make someone else admin first.')
    }
    throw new ApiError(404, 'Member not found')
  }

  async listConnections(tenantId: string): Promise<Connection[]> {
    await this.tick()
    this.tenant(tenantId)
    return structuredClone(this.state.connections[tenantId] ?? [])
  }

  async createConnection(tenantId: string, body: ConnectionCreate): Promise<ConnectionSetup> {
    await this.tick()
    this.tenant(tenantId)
    if (!/^\d{12}$/.test(body.awsAccountId)) {
      throw new ApiError(422, 'Request validation failed', [
        {
          field: 'awsAccountId',
          message: 'An AWS account ID has exactly 12 digits',
        },
      ])
    }
    const all = Object.values(this.state.connections).flat()
    const own = (this.state.connections[tenantId] ?? []).find((c) => c.awsAccountId === body.awsAccountId)
    if (own && own.status !== 'disconnected') {
      throw new ApiError(
        409,
        'This AWS account is already connected to this workspace. Use Set up again or Disconnect on the Connections page.',
      )
    }
    if (own) {
      // Reconnect: same connection, fresh External ID, back to pending.
      Object.assign(own, {
        accountType: body.accountType,
        createCurExport: body.createCurExport,
        status: 'pending',
        curBucket: null,
        curPrefix: null,
        curExportArn: null,
        permissionChecks: [],
        lastError: null,
        lastValidatedAt: null,
      })
      this.state.externalIds[own.id] = externalId()
      this.save()
      return this.setup(own)
    }
    if (all.some((c) => c.awsAccountId === body.awsAccountId && c.status !== 'disconnected')) {
      throw new ApiError(409, 'This AWS account is already connected to a Cloudoptics workspace.')
    }
    const conn: Connection = {
      id: randomId(),
      awsAccountId: body.awsAccountId,
      accountType: body.accountType,
      roleArn: `arn:aws:iam::${body.awsAccountId}:role/CloudopticsBillingReadOnly`,
      status: 'pending',
      createCurExport: body.createCurExport,
      curBucket: null,
      curPrefix: null,
      curExportArn: null,
      permissionChecks: [],
      lastError: null,
      lastValidatedAt: null,
      createdAt: new Date().toISOString(),
      stackName: 'cloudoptics-billing-access',
      stackVersion: null,
    }
    ;(this.state.connections[tenantId] ??= []).push(conn)
    this.state.externalIds[conn.id] = externalId()
    this.save()
    return this.setup(conn)
  }

  async createIntent(tenantId: string): Promise<IntentSetup> {
    await this.tick()
    this.tenant(tenantId)
    const id = randomId()
    ;(this.state.intents ??= {})[id] = { tenantId, createdAt: Date.now(), connectionId: null }
    this.state.externalIds[id] = externalId()
    this.save()
    const base = `${import.meta.env.BASE_URL}cfn`
    return {
      intent: { id, status: 'waiting', connectionId: null, error: null },
      // No S3-hosted template in the demo: the stack "reports back" by itself after a few seconds.
      quickCreateUrl: null,
      templateUrl: `${base}/cloudoptics-access.yaml`,
      templateDownloadUrl: `${base}/cloudoptics-access.yaml`,
      externalId: this.state.externalIds[id] ?? '',
      principalArn: 'arn:aws:iam::000000000000:role/CloudopticsBroker',
      resourceTemplateUrl: `${base}/resource-role.yaml`,
      callbackUrl: 'https://cloudoptics.example/api/v1/cfn/callback',
      connectionRef: `i:${id}`,
      stackRegion: 'us-east-1',
    }
  }

  async getIntent(tenantId: string, intentId: string): Promise<ConnectionIntent> {
    await this.tick()
    const intent = this.state.intents?.[intentId]
    if (!intent || intent.tenantId !== tenantId) throw new ApiError(404, 'Link not found')
    if (!intent.connectionId && Date.now() - intent.createdAt >= this.latencyMs * 24) {
      const account = Array.from(crypto.getRandomValues(new Uint8Array(12)), (b) => b % 10).join('')
      const conn: Connection = {
        id: randomId(),
        awsAccountId: account,
        accountType: 'standalone',
        roleArn: `arn:aws:iam::${account}:role/CloudopticsBillingReadOnly`,
        status: 'pending',
        createCurExport: true,
        curBucket: null,
        curPrefix: null,
        curExportArn: null,
        permissionChecks: [],
        lastError: null,
        lastValidatedAt: null,
        createdAt: new Date().toISOString(),
        stackName: 'cloudoptics-access',
        stackVersion: '2.0.3',
      }
      ;(this.state.connections[tenantId] ??= []).push(conn)
      this.state.externalIds[conn.id] = this.state.externalIds[intentId] ?? externalId()
      ;(this.state.resourceAccess ??= {})[conn.id] = true
      intent.connectionId = conn.id
      this.save()
      await this.validateConnection(tenantId, conn.id)
    }
    return {
      id: intentId,
      status: intent.connectionId ? 'connected' : 'waiting',
      connectionId: intent.connectionId,
      error: null,
    }
  }

  async disconnectConnection(tenantId: string, connectionId: string): Promise<void> {
    await this.tick()
    this.connection(tenantId, connectionId).status = 'disconnected'
    this.save()
  }

  async scanResources(tenantId: string, connectionId: string): Promise<ResourceScan> {
    await delay(this.latencyMs * 4)
    const conn = this.connection(tenantId, connectionId)
    if (conn.status !== 'healthy' && conn.status !== 'degraded') {
      throw new ApiError(409, 'Check the billing connection first: resources are scanned for working connections.')
    }
    // In the demo the stack is always found: the demo cannot see a real AWS account.
    ;(this.state.resourceAccess ??= {})[conn.id] = true
    // Like the real worker, the scan runs on after this returns; resourceSummary shows it.
    ;(this.state.scanStartedAt ??= {})[conn.id] = new Date().toISOString()
    this.save()
    const accounts = (await this.listAccounts(tenantId)).accounts.filter((a) => a.connectionId === conn.id)
    return {
      status: this.scanMs ? 'queued' : 'succeeded',
      accounts: accounts.map((a) => ({ awsAccountId: a.awsAccountId, access: true, error: null })),
    }
  }

  async reloadBills(tenantId: string, connectionId: string, period: string | null): Promise<ReloadStatus> {
    await this.tick()
    const conn = this.connection(tenantId, connectionId)
    if ((conn.status !== 'healthy' && conn.status !== 'degraded') || !conn.curBucket) {
      throw new ApiError(409, 'Check the connection first: bills are reloaded for a working connection with an export.')
    }
    const last = this.state.reloads?.[connectionId]
    if (last && Date.now() - Date.parse(last.requestedAt) < RELOAD_EVERY_MS) {
      const next = new Date(Date.parse(last.requestedAt) + RELOAD_EVERY_MS)
      throw new ApiError(
        429,
        `Bills for this connection were reloaded less than an hour ago. Try again after ${next.toISOString().slice(11, 16)} UTC.`,
      )
    }
    ;(this.state.reloads ??= {})[connectionId] = { requestedAt: new Date().toISOString(), period }
    this.save()
    // Like the worker: queued now, the result a few seconds later (reloadStatus).
    const status = await this.reloadStatus(tenantId, connectionId)
    return this.reloadMs ? { ...status, status: 'queued' } : status
  }

  async reloadStatus(tenantId: string, connectionId: string): Promise<ReloadStatus> {
    await this.tick()
    this.connection(tenantId, connectionId)
    const none: ReloadStatus = {
      connectionId, status: 'none', period: null, requestedAt: null, finishedAt: null, periods: [], error: null, nextAllowedAt: null,
    }
    const last = this.state.reloads?.[connectionId]
    if (!last) return none
    const requested = Date.parse(last.requestedAt)
    const next = requested + RELOAD_EVERY_MS
    const base = {
      ...none,
      period: last.period,
      requestedAt: last.requestedAt,
      nextAllowedAt: Date.now() < next ? new Date(next).toISOString() : null,
    }
    if (Date.now() - requested < this.reloadMs) return { ...base, status: 'running' }
    // The demo bucket has the current month and the ended ones the Cost Explorer shows.
    const inBucket = [new Date().toISOString().slice(0, 7), ...this.pastMonths().map((m) => m.month)]
    const asked = last.period ? [last.period] : [...inBucket].reverse()
    const periods: PeriodReload[] = asked.map((month, i) =>
      inBucket.includes(month)
        ? {
            period: month,
            // The months loaded before resource costs existed get them; the rest read the same.
            outcome: i === 0 || last.period ? 'reloaded' : 'unchanged',
            version: `${month}-demo`,
            detail: null,
          }
        : { period: month, outcome: 'not_in_bucket', version: null, detail: null },
    )
    const good = periods.some((p) => p.outcome === 'reloaded' || p.outcome === 'unchanged')
    return {
      ...base,
      status: good ? 'succeeded' : 'failed',
      finishedAt: new Date(requested + this.reloadMs).toISOString(),
      periods,
    }
  }

  async resourceSummary(tenantId: string): Promise<InventorySummary> {
    await this.tick()
    const scans: LastScan[] = []
    for (const conn of this.state.connections[tenantId] ?? []) {
      const accounts = (await this.listAccounts(tenantId)).accounts.filter((a) => a.connectionId === conn.id)
      const started = this.state.scanStartedAt?.[conn.id]
      if (started) {
        const elapsed = Date.now() - Date.parse(started)
        if (elapsed < this.scanMs) {
          const total = 17 * accounts.length
          scans.push({
            connectionId: conn.id,
            status: 'running',
            startedAt: started,
            finishedAt: null,
            resources: 0,
            accounts: accounts.length,
            accountsWithAccess: 0,
            regions: 0,
            errors: [],
            progress: { regionsDone: Math.floor((total * elapsed) / this.scanMs), regionsTotal: total },
          })
          continue
        }
        ;(this.state.scannedAt ??= {})[conn.id] = new Date(Date.parse(started) + this.scanMs).toISOString()
        delete this.state.scanStartedAt?.[conn.id]
        this.save()
      }
      const at = this.state.scannedAt?.[conn.id]
      if (!at) continue
      scans.push({
        connectionId: conn.id,
        status: 'succeeded' as const,
        startedAt: at,
        finishedAt: at,
        resources: 12,
        accounts: accounts.length,
        accountsWithAccess: accounts.length,
        regions: 17 * accounts.length,
        errors: [],
      })
    }
    return { lastScanAt: scans[0]?.finishedAt ?? null, counts: [], scans }
  }

  async getSetup(tenantId: string, connectionId: string): Promise<ConnectionSetup> {
    await this.tick()
    return this.setup(this.connection(tenantId, connectionId))
  }

  async validateConnection(tenantId: string, connectionId: string): Promise<Connection> {
    await delay(this.latencyMs * 4)
    const conn = this.connection(tenantId, connectionId)
    const bucket = `cloudoptics-cur-${conn.awsAccountId}`
    const checks: PermissionCheck[] = [
      {
        id: 'assume_role',
        label: 'Assume the Cloudoptics role',
        status: 'ok',
        message: 'Role assumed.',
        hint: null,
      },
      {
        id: 'cost_explorer',
        label: 'Read Cost Explorer',
        status: 'ok',
        message: 'Cost data readable.',
        hint: null,
      },
      {
        id: 'cur_export',
        label: 'Find the CUR 2.0 export',
        status: 'ok',
        message: 'Found export cloudoptics-cur2.',
        hint: null,
      },
      {
        id: 'cur_delivery',
        label: 'Read the export bucket',
        status: 'warning',
        message: 'The export exists; its first delivery usually arrives within 24 hours.',
        hint: 'We will check again automatically.',
      },
      {
        id: 'organization',
        label: 'Check the account type',
        status: 'ok',
        message: 'Account type confirmed.',
        hint: null,
      },
      {
        id: 'savings_plans',
        label: 'Read Savings Plans',
        status: 'ok',
        message: 'Savings Plans readable.',
        hint: null,
      },
    ]
    Object.assign(conn, {
      status: 'healthy',
      permissionChecks: checks,
      curBucket: bucket,
      curPrefix: 'cloudoptics',
      curExportArn: `arn:aws:bcm-data-exports:us-east-1:${conn.awsAccountId}:export/cloudoptics-cur2-demo`,
      lastError: null,
      lastValidatedAt: new Date().toISOString(),
    })
    this.save()
    return structuredClone(conn)
  }

  /** The demo's ended months with bills, newest first (the last one matches its monthly report). */
  private pastMonths(): { month: string; total: number }[] {
    const now = new Date()
    return DEMO_MONTH_TOTALS.map((total, i) => ({
      month: new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1 - i, 1)).toISOString().slice(0, 7),
      total,
    }))
  }

  /** An ended month the demo has bills for, or null for the current month. */
  private pastMonth(month: string | null | undefined): { month: string; total: number; index: number } | null {
    if (!month || month >= new Date().toISOString().slice(0, 7)) return null
    const index = this.pastMonths().findIndex((m) => m.month === month)
    return { month, total: index >= 0 ? this.pastMonths()[index]!.total : 0, index }
  }

  async costMonths(tenantId: string): Promise<CostMonths> {
    await this.tick()
    this.tenant(tenantId)
    if (!this.hasData(tenantId)) return { currency: 'USD', months: [] }
    const summary = await this.costSummary(tenantId)
    const made = this.state.reportsMade?.[tenantId] ?? {}
    const lastDay = (month: string) =>
      new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).toISOString().slice(0, 10)
    return {
      currency: 'USD',
      months: [
        {
          month: summary.month,
          source: 'cur',
          total: summary.monthToDate?.effective ?? 0,
          dataThrough: summary.dataThrough ?? lastDay(summary.month),
          state: 'current',
          report: null,
        },
        ...this.pastMonths().map(({ month, total }) => ({
          month,
          source: 'cur' as const,
          total,
          dataThrough: lastDay(month),
          state: demoMonthState(month),
          report: made[month] ? ('stored' as const) : ('draft' as const),
        })),
      ],
    }
  }

  async costSummary(tenantId: string, month?: string | null): Promise<CostSummary> {
    await this.tick()
    this.tenant(tenantId)
    const now = new Date()
    const current = now.toISOString().slice(0, 7)
    const connected = (this.state.connections[tenantId] ?? []).filter((c) => c.status !== 'disconnected')
    const past = this.pastMonth(month)
    const empty: CostSummary = {
      status: connected.length ? 'waiting' : 'no_connection',
      source: null,
      month: past?.month ?? current,
      currency: 'USD',
      dataThrough: null,
      lastLoadAt: null,
      lastLoadStatus: null,
      monthToDate: null,
      forecast: null,
      previousMonth: null,
      monthState: past ? demoMonthState(past.month) : 'current',
    }
    if (past) {
      if (!this.hasData(tenantId) || past.index < 0) return empty
      const before = this.pastMonths()[past.index + 1]?.total ?? null
      const lastDay = new Date(Date.UTC(Number(past.month.slice(0, 4)), Number(past.month.slice(5, 7)), 0))
      return {
        ...empty,
        status: 'ready',
        source: 'cur',
        dataThrough: lastDay.toISOString().slice(0, 10),
        lastLoadAt: now.toISOString(),
        lastLoadStatus: 'succeeded',
        monthToDate: { effective: past.total, billed: Math.round((past.total + 60) * 100) / 100, credits: -60 },
        previousMonth: before,
      }
    }
    if (!this.hasData(tenantId)) return empty
    // Illustrative figures in the shape of tools/cur-fixture: about USD 41 a day.
    const days = Math.max(now.getUTCDate() - 1, 1)
    const daysInMonth = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + 1, 0)).getUTCDate()
    const daily = 41.26
    const through = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), days))
    return {
      ...empty,
      status: 'ready',
      source: 'cur',
      dataThrough: through.toISOString().slice(0, 10),
      lastLoadAt: now.toISOString(),
      lastLoadStatus: 'succeeded',
      monthToDate: {
        effective: daily * days,
        billed: daily * days,
        credits: -2 * days,
      },
      forecast: {
        amount: daily * daysInMonth,
        method: 'run_rate',
        daysCovered: days,
        daysInMonth,
        runRateDays: Math.min(days, 7),
        dailyRunRate: daily,
        // AWS Support (Developer) is billed on the 1st, so it is already in the month so far.
        monthlyCharges: 0,
      },
      previousMonth: DEMO_MONTH_TOTALS[0]!,
    }
  }

  async costDaily(tenantId: string, days = 30, month?: string | null): Promise<DailyCosts> {
    await this.tick()
    this.tenant(tenantId)
    if (!this.hasData(tenantId)) return { source: null, currency: 'USD', points: [] }
    const past = this.pastMonth(month)
    if (past) {
      if (past.index < 0) return { source: null, currency: 'USD', points: [] }
      // Every day of the month, the same weekly shape, scaled to the month's total.
      const year = Number(past.month.slice(0, 4))
      const m = Number(past.month.slice(5, 7)) - 1
      const count = new Date(Date.UTC(year, m + 1, 0)).getUTCDate()
      const shape = Array.from({ length: count }, (_, i) => {
        const weekday = new Date(Date.UTC(year, m, i + 1)).getUTCDay()
        return (weekday === 0 || weekday === 6 ? 31.4 : 45.2) + ((i * 7) % 5) * 0.9
      })
      const scale = past.total / shape.reduce((sum, v) => sum + v, 0)
      return {
        source: 'cur',
        currency: 'USD',
        points: shape.map((v, i) => ({
          date: new Date(Date.UTC(year, m, i + 1)).toISOString().slice(0, 10),
          amount: Math.round(v * scale * 100) / 100,
        })),
      }
    }
    const today = Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate())
    const points = Array.from({ length: days }, (_, i) => {
      const day = new Date(today - (days - i) * 86_400_000)
      // Weekdays cost more than weekends, like a typical dev/test estate.
      const weekend = day.getUTCDay() === 0 || day.getUTCDay() === 6
      const amount = (weekend ? 31.4 : 45.2) + ((i * 7) % 5) * 0.9
      return {
        date: day.toISOString().slice(0, 10),
        amount: Math.round(amount * 100) / 100,
      }
    })
    return { source: 'cur', currency: 'USD', points }
  }

  /** The Overview's run rate for this month, then a gently rising trend, with the report's FX split. */
  async costForecast(tenantId: string): Promise<CostForecast> {
    const summary = await this.costSummary(tenantId)
    const rate = await this.fxCurrent(tenantId)
    const empty: CostForecast = {
      status: 'no_data', currency: rate.currency, basis: rate.basis, dataThrough: null, quote: rate.quote,
      monthEnd: null, months: [], monthlyChargesUsd: 0, taxShare: 0, trendDays: 0, decomposition: null,
    }
    if (!summary.forecast || !summary.dataThrough) return empty
    const round = (n: number) => Math.round(n * 100) / 100
    const f = summary.forecast
    const half = round(Math.sqrt(f.daysInMonth - f.daysCovered) * 1.645 * 6.2)
    const now = new Date()
    const months = [1, 2, 3].map((ahead) => {
      const first = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() + ahead, 1))
      const days = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate()
      const amount = round(41.26 * 1.015 ** ahead * days + 29)
      const width = round(amount * 0.05 * ahead)
      return { month: first.toISOString().slice(0, 7), amount, low: round(amount - width), high: round(amount + width), method: 'trend' as const }
    })
    const report = await this.demoReport(tenantId, this.lastFullMonth())
    const before = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 2, 1)).toISOString().slice(0, 7)
    const s = report.spend
    return {
      ...empty,
      status: 'ready',
      dataThrough: summary.dataThrough,
      monthEnd: { month: summary.month, forecast: f, low: round(Math.max(f.amount - half, 0)), high: round(f.amount + half) },
      months,
      monthlyChargesUsd: 29,
      trendDays: 90,
      decomposition: {
        month: report.month,
        previousMonth: before,
        usd: s.usd,
        previousUsd: s.previousUsd,
        local: s.local,
        previousLocal: s.previousLocal,
        rate: s.rate,
        previousRate: s.previousRate,
        usageEffect: s.usageEffect,
        fxEffect: s.fxEffect,
      },
    }
  }

  /** Month to date against the same days last month (an ended month: whole months), for the demo estate. */
  async costChanges(tenantId: string, month?: string | null): Promise<CostChanges> {
    await this.tick()
    this.tenant(tenantId)
    const empty: CostChanges = {
      status: 'no_data', month: null, current: null, previous: null, usd: 0, previousUsd: 0, changeUsd: 0, rises: [], falls: [],
    }
    if (!this.hasData(tenantId)) return empty
    const past = this.pastMonth(month)
    if (past && past.index < 0) return empty
    const summary = await this.costSummary(tenantId, past?.month)
    const to = summary.dataThrough ?? isoDaysAgo(1)
    const from = `${to.slice(0, 8)}01`
    const prev = new Date(Date.UTC(Number(to.slice(0, 4)), Number(to.slice(5, 7)) - 2, 1))
    const prevEnd = new Date(Date.UTC(prev.getUTCFullYear(), prev.getUTCMonth() + 1, 0)).getUTCDate()
    const prevMonth = prev.toISOString().slice(0, 8)
    const prevTo = `${prevMonth}${String(past ? prevEnd : Math.min(Number(to.slice(8, 10)), prevEnd)).padStart(2, '0')}`
    const share = Math.max(Number(to.slice(8, 10)), 1) / 30
    const item = (
      serviceCode: string, serviceName: string, region: string | null, usd: number, previousUsd: number, usageType: string,
    ): CostContributor => {
      const r = (n: number) => Math.round(n * share * 100) / 100
      const [cur, before] = [r(usd), r(previousUsd)]
      const change = Math.round((cur - before) * 100) / 100
      const pct = Math.round((1000 * change) / before) / 10
      const where = region ? `${serviceName} in ${region}` : serviceName
      return {
        serviceCode, serviceName, region, usd: cur, previousUsd: before, changeUsd: change, changePercent: pct,
        usageType, usageTypeChangeUsd: change,
        sentence: `${where} ${change > 0 ? 'rose' : 'fell'} USD ${Math.abs(change).toFixed(2)} (${pct > 0 ? '+' : ''}${pct.toFixed(0)}%) mainly due to ${usageType}.`,
      }
    }
    const rises = [
      item('AmazonEC2', 'Amazon Elastic Compute Cloud', 'eu-west-1', 512.4, 426.0, 'EU-BoxUsage:m5.xlarge'),
      item('AmazonRDS', 'Amazon Relational Database Service', 'af-south-1', 214.9, 183.7, 'AFS1-InstanceUsage:db.t3.medium'),
      item('AmazonCloudWatch', 'AmazonCloudWatch', null, 24.1, 18.0, 'CW:MetricMonitorUsage'),
    ]
    const falls = [
      item('AmazonS3', 'Amazon Simple Storage Service', 'eu-west-1', 88.3, 101.05, 'EU-TimedStorage-ByteHrs'),
      item('AWSELB', 'Elastic Load Balancing', 'eu-west-1', 61.7, 70.1, 'EU-LoadBalancerUsage'),
    ]
    const usd = Math.round(901.4 * share * 100) / 100
    const previousUsd = Math.round(798.85 * share * 100) / 100
    return {
      status: 'ready',
      month: to.slice(0, 7),
      current: { from, to },
      previous: { from: `${prevMonth}01`, to: prevTo },
      usd,
      previousUsd,
      changeUsd: Math.round((usd - previousUsd) * 100) / 100,
      rises,
      falls,
    }
  }

  // The demo estate has fewer items than any limit, so ``limit`` changes nothing here.
  async costBreakdown(tenantId: string, dimension: CostDimension, _limit = 8, requested?: string | null): Promise<CostBreakdown> {
    await this.tick()
    this.tenant(tenantId)
    const past = this.pastMonth(requested)
    const month = past?.month ?? new Date().toISOString().slice(0, 7)
    if (!this.hasData(tenantId) || (past && past.index < 0))
      return {
        source: null,
        month,
        dimension,
        currency: 'USD',
        total: 0,
        items: [],
      }
    const rows: Record<CostDimension, [string, string, number][]> = {
      service: [
        ['AmazonEC2', 'Amazon Elastic Compute Cloud', 512.4],
        ['AmazonRDS', 'Amazon Relational Database Service', 214.9],
        ['AmazonS3', 'Amazon Simple Storage Service', 88.3],
        ['AWSELB', 'Elastic Load Balancing', 61.7],
        ['AmazonCloudWatch', 'AmazonCloudWatch', 24.1],
      ],
      account: [
        ['111111111111', 'demo-prod', 598.2],
        ['333333333333', 'demo-dev', 303.2],
      ],
      region: [
        ['eu-west-1', 'eu-west-1', 640.8],
        ['af-south-1', 'af-south-1', 236.5],
        ['global', 'Global', 24.1],
      ],
    }
    // An ended month: the same estate, scaled to the month's spend excluding tax (about 3%).
    const scale = past ? (past.total * 0.97) / rows[dimension].reduce((sum, [, , amount]) => sum + amount, 0) : 1
    if (scale !== 1) rows[dimension] = rows[dimension].map(([k, l, a]) => [k, l, Math.round(a * scale * 100) / 100])
    const total = rows[dimension].reduce((sum, [, , amount]) => sum + amount, 0)
    return {
      source: 'cur',
      month,
      dimension,
      currency: 'USD',
      total: Math.round(total * 100) / 100,
      items: rows[dimension].map(([key, label, amount]) => ({
        key,
        label,
        amount,
        share: Math.round((amount / total) * 10_000) / 10_000,
      })),
    }
  }

  async fxCurrent(tenantId: string): Promise<CurrentRate> {
    await this.tick()
    const tenant = this.tenant(tenantId)
    const currency = tenant.displayCurrency
    const rate = currency === 'USD' ? 1 : DEMO_RATES[currency]
    return {
      currency,
      basis: 'official',
      quote: rate
        ? {
            currency,
            rate,
            rateDate: new Date().toISOString().slice(0, 10),
            basis: 'official',
            source: 'manual',
            estimated: false,
          }
        : null,
    }
  }

  async listAccounts(tenantId: string): Promise<AccountList> {
    await this.tick()
    this.tenant(tenantId)
    const now = new Date().toISOString()
    const accounts: CloudAccount[] = []
    for (const c of this.state.connections[tenantId] ?? []) {
      if (c.status === 'disconnected' || c.status === 'pending') continue
      const usable = c.status === 'healthy' || c.status === 'degraded'
      const own: CloudAccount = {
        connectionId: c.id,
        awsAccountId: c.awsAccountId,
        name: c.accountType === 'management' ? 'payer' : null,
        accountType: c.accountType,
        orgStatus: c.accountType === 'management' && usable ? 'ACTIVE' : null,
        orgSeenAt: c.accountType === 'management' && usable ? now : null,
        resourceAccess: usable && (this.state.resourceAccess?.[c.id] ?? false),
        hasCostData: usable,
      }
      accounts.push(own)
      if (c.accountType === 'management' && usable) {
        accounts.push(
          {
            ...own,
            awsAccountId: '111111111111',
            name: 'demo-prod',
            accountType: 'member',
          },
          {
            ...own,
            awsAccountId: '333333333333',
            name: 'demo-dev',
            accountType: 'member',
          },
          {
            ...own,
            awsAccountId: '444444444444',
            name: 'sandbox',
            accountType: 'member',
            hasCostData: false,
          },
        )
      }
    }
    const current = accounts.filter((a) => a.orgStatus !== 'NOT_LISTED')
    return {
      coverage: {
        accounts: current.length,
        withCostData: current.filter((a) => a.hasCostData).length,
        withResourceAccess: current.filter((a) => a.resourceAccess).length,
      },
      accounts,
    }
  }

  private recommendations(tenantId: string): Recommendation[] {
    this.tenant(tenantId)
    if (!this.hasData(tenantId)) return []
    const settings = this.ruleSettings(tenantId)
    const disabled = settings.disabledRules
    this.settleVerifications(tenantId)
    const payer = this.payer(tenantId)
    const base = [...DEMO_RECS, ...demoScheduleRecs(this.tenant(tenantId).timeZone, settings), ...DEMO_VERIFIED]
    const all = payer ? [...base, demoR8(this.advisory(tenantId, payer))] : base
    return all
      .filter((r) => !disabled.includes(r.ruleId) || r.status === 'verified')
      .map((r) => {
        const key = `${tenantId}:${r.id}`
        const status = this.state.recommendationStatus[key] ?? r.status ?? 'new'
        const rec = { ...demoRecommendation(r, status), assigneeId: this.state.recommendationAssignee?.[key] ?? null }
        const doneAt = this.state.recommendationDoneAt?.[key]
        if (!doneAt || (status !== 'done' && status !== 'verified') || r.status === 'verified') return rec
        const verifiedAt = new Date(Date.parse(doneAt) + DEMO_VERIFY_MS).toISOString()
        return {
          ...rec,
          doneAt,
          ...(status === 'verified'
            ? {
                verifiedAt,
                changedAt: doneAt,
                verification: {
                  check: DEMO_CHECKS[r.ruleId] ?? 'rule_no_longer_applies',
                  outcome: 'implemented' as const,
                  checkedAt: verifiedAt,
                  estimateUsdMonth: rec.savingsUsdMonth,
                },
                realisedSavingsUsdMonth: null,
                realisedBasis: null,
              }
            : {}),
        }
      })
  }

  /** The demo's stand-in for the verification job: done for a few seconds means verified. */
  private settleVerifications(tenantId: string): void {
    const prefix = `${tenantId}:`
    for (const [key, at] of Object.entries(this.state.recommendationDoneAt ?? {})) {
      if (!key.startsWith(prefix) || this.state.recommendationStatus[key] !== 'done') continue
      if (Date.now() - Date.parse(at) < DEMO_VERIFY_MS) continue
      this.state.recommendationStatus[key] = 'verified'
      const events = (this.state.recommendationEvents ??= {})
      events[key] = [
        ...(events[key] ?? []),
        {
          id: randomId(),
          createdAt: new Date(Date.parse(at) + DEMO_VERIFY_MS).toISOString(),
          actorId: null,
          actorName: null,
          kind: 'status_changed',
          fromStatus: 'done',
          toStatus: 'verified',
          assigneeId: null,
          assigneeName: null,
          body: 'Checked in AWS. The change is in place.',
        },
      ]
      this.save()
    }
  }

  private ruleSettings(tenantId: string): RuleSettings {
    // Settings saved before a field existed take its default.
    return { ...DEFAULT_RULE_SETTINGS, ...this.state.ruleSettings?.[tenantId] }
  }

  private addEvent(tenantId: string, id: string, event: Omit<RecommendationEvent, 'id' | 'createdAt' | 'actorId' | 'actorName'>): void {
    const me = this.requireMe()
    const key = `${tenantId}:${id}`
    const events = (this.state.recommendationEvents ??= {})
    events[key] = [
      ...(events[key] ?? []),
      { id: randomId(), createdAt: new Date().toISOString(), actorId: me.id, actorName: me.displayName ?? me.email, ...event },
    ]
  }

  async listRecommendations(tenantId: string, query: RecommendationQuery = {}): Promise<Recommendation[]> {
    await this.tick()
    return this.recommendations(tenantId)
      .filter((r) => query.status === 'all' || OPEN.includes(r.status))
      .sort((a, b) => b.priorityScore - a.priorityScore)
  }

  async recommendationSummary(tenantId: string): Promise<RecommendationSummary> {
    await this.tick()
    const open = this.recommendations(tenantId).filter((r) => OPEN.includes(r.status))
    const byRule = new Map<string, { ruleId: string; count: number; savingsUsdMonth: number }>()
    for (const r of open) {
      const row = byRule.get(r.ruleId) ?? {
        ruleId: r.ruleId,
        count: 0,
        savingsUsdMonth: 0,
      }
      row.count += 1
      row.savingsUsdMonth += r.savingsUsdMonth
      byRule.set(r.ruleId, row)
    }
    const all = this.recommendations(tenantId)
    const verified = all.filter((r) => r.status === 'verified')
    const round = (n: number) => Math.round(n * 100) / 100
    return {
      openCount: open.length,
      savingsUsdMonth: Math.round(open.reduce((sum, r) => sum + r.savingsUsdMonth, 0) * 100) / 100,
      byRule: [...byRule.values()].sort((a, b) => b.savingsUsdMonth - a.savingsUsdMonth),
      lastEvaluatedAt: open[0]?.lastEvaluatedAt ?? null,
      verifiedCount: verified.length,
      awaitingVerificationCount: all.filter((r) => r.status === 'done').length,
      notVerifiedCount: 0,
      realisedSavingsUsdMonth: round(
        verified.filter((r) => r.realisedBasis === 'cur').reduce((sum, r) => sum + (r.realisedSavingsUsdMonth ?? 0), 0),
      ),
      estimatedRealisedUsdMonth: round(
        verified
          .filter((r) => r.realisedBasis !== 'cur')
          .reduce((sum, r) => sum + (r.realisedSavingsUsdMonth ?? r.verification?.estimateUsdMonth ?? r.savingsUsdMonth), 0),
      ),
    }
  }

  async getRecommendation(tenantId: string, id: string): Promise<Recommendation> {
    await this.tick()
    const rec = this.recommendations(tenantId).find((r) => r.id === id)
    if (!rec) throw new ApiError(404, 'Recommendation not found')
    return rec
  }

  async updateRecommendation(tenantId: string, id: string, change: RecommendationChange): Promise<Recommendation> {
    if (change.status === 'dismissed' && !change.reason?.trim()) {
      throw new ApiError(422, 'A reason is required to dismiss a recommendation.')
    }
    const before = await this.getRecommendation(tenantId, id)
    const key = `${tenantId}:${id}`
    let status: RecommendationStatus | undefined = change.status
    if (change.assigneeId !== undefined && change.assigneeId !== before.assigneeId) {
      ;(this.state.recommendationAssignee ??= {})[key] = change.assigneeId
      const member = (await this.listMembers(tenantId)).find((m) => m.userId === change.assigneeId)
      this.addEvent(tenantId, id, {
        kind: 'assigned',
        fromStatus: null,
        toStatus: null,
        assigneeId: change.assigneeId,
        assigneeName: member ? (member.displayName ?? member.email) : null,
        body: null,
      })
      if (!status && change.assigneeId && before.status === 'new') status = 'assigned'
      if (!status && change.assigneeId === null && before.status === 'assigned') status = 'new'
    }
    if (status && status !== before.status) {
      this.state.recommendationStatus[key] = status
      const doneAt = (this.state.recommendationDoneAt ??= {})
      if (status === 'done') doneAt[key] = new Date().toISOString()
      else delete doneAt[key]
      this.addEvent(tenantId, id, {
        kind: 'status_changed',
        fromStatus: before.status,
        toStatus: status,
        assigneeId: null,
        assigneeName: null,
        body: change.reason ?? null,
      })
    }
    this.save()
    return this.getRecommendation(tenantId, id)
  }

  async updateRecommendations(tenantId: string, ids: string[], change: RecommendationChange): Promise<{ updated: number }> {
    let updated = 0
    for (const id of new Set(ids)) {
      try {
        await this.updateRecommendation(tenantId, id, change)
        updated += 1
      } catch (e) {
        if (!(e instanceof ApiError) || e.status !== 404) throw e
      }
    }
    return { updated }
  }

  async recommendationEvents(tenantId: string, id: string): Promise<RecommendationEvent[]> {
    await this.getRecommendation(tenantId, id)
    return structuredClone(this.state.recommendationEvents?.[`${tenantId}:${id}`] ?? [])
  }

  // Demo resources are volumes, addresses, load balancers and billing-only findings: no usage series.
  async recommendationUsage(tenantId: string, id: string): Promise<UsageSeries[]> {
    await this.getRecommendation(tenantId, id)
    return []
  }

  async addComment(tenantId: string, id: string, body: string): Promise<RecommendationEvent[]> {
    await this.getRecommendation(tenantId, id)
    this.addEvent(tenantId, id, {
      kind: 'commented',
      fromStatus: null,
      toStatus: null,
      assigneeId: null,
      assigneeName: null,
      body: body.trim(),
    })
    this.save()
    return this.recommendationEvents(tenantId, id)
  }

  private anomalyView(tenantId: string, a: DemoAnomaly): Anomaly {
    const stored = this.state.anomalyStatus?.[`${tenantId}:${a.id}`]
    const date = isoDaysAgo(a.daysAgo)
    const round = (n: number) => Math.round(n * 100) / 100
    return {
      id: a.id,
      date,
      accountId: a.accountId,
      accountName: a.accountName,
      serviceCode: a.serviceCode,
      serviceName: a.serviceName,
      actualUsd: a.spike,
      expectedUsd: a.base,
      impactUsd: round(a.spike - a.base),
      deviationPercent: Math.round((1000 * (a.spike - a.base)) / a.base) / 10,
      contributors: structuredClone(a.contributors),
      status: stored?.status ?? a.status,
      suppressed: false,
      note: stored ? stored.note : a.note,
      statusChangedAt: stored?.at ?? (a.status === 'open' ? null : `${isoDaysAgo(a.daysAgo - 1)}T09:12:00Z`),
      statusChangedBy: stored || a.status !== 'open' ? (this.state.me?.displayName ?? null) : null,
      detectedAt: `${isoDaysAgo(a.daysAgo - 1)}T07:40:00Z`,
      alerted: true,
    }
  }

  async listAnomalies(tenantId: string, status?: AnomalyStatus): Promise<Anomaly[]> {
    await this.tick()
    this.tenant(tenantId)
    if (!this.hasData(tenantId)) return []
    return DEMO_ANOMALIES.map((a) => this.anomalyView(tenantId, a)).filter((a) => !status || a.status === status)
  }

  async getAnomaly(tenantId: string, id: string): Promise<AnomalyDetail> {
    await this.tick()
    this.tenant(tenantId)
    const a = DEMO_ANOMALIES.find((x) => x.id === id)
    if (!a || !this.hasData(tenantId)) throw new ApiError(404, 'Anomaly not found')
    const series = Array.from({ length: 28 + Math.min(7, a.daysAgo) }, (_, i) => {
      const daysAgo = a.daysAgo + 27 - i
      const day = isoDaysAgo(daysAgo)
      const weekend = [0, 6].includes(new Date(`${day}T00:00:00Z`).getUTCDay())
      const expected = Math.round(a.base * (weekend ? 0.7 : 1) * 100) / 100
      const wobble = ((i * 7) % 5) * 0.04 * a.base
      const actual = daysAgo === a.daysAgo ? a.spike : Math.round((expected + wobble - 0.08 * a.base) * 100) / 100
      return { date: day, actual, expected, upper: Math.round(expected * 1.3 * 100) / 100 }
    })
    return { ...this.anomalyView(tenantId, a), series }
  }

  async updateAnomaly(
    tenantId: string,
    id: string,
    change: { status: AnomalyStatus; note?: string | null },
  ): Promise<AnomalyDetail> {
    this.tenant(tenantId)
    if (!DEMO_ANOMALIES.some((a) => a.id === id)) throw new ApiError(404, 'Anomaly not found')
    ;(this.state.anomalyStatus ??= {})[`${tenantId}:${id}`] = {
      status: change.status,
      note: change.note?.trim() || null,
      at: new Date().toISOString(),
    }
    this.save()
    return this.getAnomaly(tenantId, id)
  }

  async getNotificationPreferences(tenantId: string): Promise<NotificationPreferences> {
    await this.tick()
    this.tenant(tenantId)
    const defaults = {
      weeklyDigest: true,
      anomalyAlert: true,
      monthlyReport: true,
      commitmentExpiry: true,
      ...this.state.notificationDefaults?.[tenantId],
    }
    return { mine: { ...defaults, ...this.state.notificationsMine?.[tenantId] }, defaults }
  }

  async saveMyNotifications(tenantId: string, change: Partial<NotificationChoices>): Promise<NotificationPreferences> {
    this.tenant(tenantId)
    const store = (this.state.notificationsMine ??= {})
    store[tenantId] = { ...store[tenantId], ...change }
    this.save()
    return this.getNotificationPreferences(tenantId)
  }

  async saveNotificationDefaults(tenantId: string, change: Partial<NotificationChoices>): Promise<NotificationPreferences> {
    this.tenant(tenantId)
    const store = (this.state.notificationDefaults ??= {})
    store[tenantId] = { ...store[tenantId], ...change }
    this.save()
    return this.getNotificationPreferences(tenantId)
  }

  /** A digest built from the demo's daily costs and recommendations, as the API words it. */
  async digestPreview(tenantId: string): Promise<DigestPreview> {
    const tenant = this.tenant(tenantId)
    if (!this.hasData(tenantId)) throw new ApiError(404, 'No bills for last week yet')
    const days = (await this.costDaily(tenantId, 21)).points
    const today = new Date(`${isoDaysAgo(0)}T00:00:00Z`)
    const weekStart = isoDaysAgo(((today.getUTCDay() + 6) % 7) + 7)
    const weekEnd = isoDaysAgo(((today.getUTCDay() + 6) % 7) + 1)
    const lastStart = isoDaysAgo(((today.getUTCDay() + 6) % 7) + 14)
    const sum = (from: string, to: string) => days.filter((p) => p.date >= from && p.date <= to).reduce((s, p) => s + p.amount, 0)
    const spend = sum(weekStart, weekEnd)
    const previous = sum(lastStart, isoDaysAgo(((today.getUTCDay() + 6) % 7) + 8))
    const usd = (n: number) => `USD ${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    const pct = previous ? Math.round((100 * (spend - previous)) / previous) : null
    const recs = (await this.listRecommendations(tenantId)).filter((r) => OPEN.includes(r.status))
    const saving = recs.reduce((s, r) => s + r.savingsUsdMonth, 0)
    const sections: [string, string[]][] = [
      ['Spend', [`Spent ${usd(spend)}, against ${usd(previous)} the week before.`]],
      [
        'Recommendations',
        [
          `${recs.length} open, worth ${usd(saving)} a month.`,
          ...recs.slice(0, 5).map((r) => `${r.title}: ${usd(r.savingsUsdMonth)} a month, ${r.riskLevel} risk, effort ${r.effort}`),
        ],
      ],
      ['Anomalies', ['1 unusual day, USD 124.20 above normal.', '1 still open.']],
    ]
    const subject = `Your AWS week: ${usd(spend)} spent${pct === null ? '' : ` (${pct >= 0 ? '+' : ''}${pct}% on the week before)`}`
    const esc = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    const fmt = (iso: string) => new Date(`${iso}T00:00:00Z`).toLocaleDateString('en-GB', { day: '2-digit', month: 'short', timeZone: 'UTC' })
    const head = `${tenant.name}: ${fmt(weekStart)} to ${fmt(weekEnd)} ${weekEnd.slice(0, 4)}`
    const text = [head, '', ...sections.flatMap(([t, lines]) => [t, ...lines.map((l) => `- ${l}`), ''])].join('\n')
    const html =
      `<p><strong>${esc(head)}</strong></p>` +
      sections.map(([t, lines]) => `<h3>${esc(t)}</h3><ul>${lines.map((l) => `<li>${esc(l)}</li>`).join('')}</ul>`).join('')
    return { weekStart, weekEnd, subject, text, html }
  }

  private lastFullMonth(): string {
    const now = new Date()
    return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1)).toISOString().slice(0, 7)
  }

  /** A report from the demo estate's figures (same services and accounts as the Overview). */
  private async demoReport(tenantId: string, month: string): Promise<MonthlyReport> {
    const tenant = this.tenant(tenantId)
    const recs = (await this.listRecommendations(tenantId)).filter((r) => OPEN.includes(r.status))
    const rate = (await this.fxCurrent(tenantId)).quote?.rate ?? null
    const usd = 1186.4
    const previousUsd = 1237.75
    const prevRate = rate === null ? null : Math.round(rate * 0.97 * 100) / 100
    const local = rate === null ? null : Math.round(usd * rate * 100) / 100
    const previousLocal = prevRate === null ? null : Math.round(previousUsd * prevRate * 100) / 100
    const usage = prevRate === null ? null : Math.round((usd - previousUsd) * prevRate * 100) / 100
    const fx = rate === null || prevRate === null ? null : Math.round(usd * (rate - prevRate) * 100) / 100
    const lastDay = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5, 7)), 0)).toISOString().slice(0, 10)
    const madeAt = this.state.reportsMade?.[tenantId]?.[month]
    return {
      month,
      workspace: tenant.name,
      currency: tenant.displayCurrency,
      generatedAt: madeAt ?? new Date().toISOString(),
      spend: {
        usd,
        local,
        rate,
        previousUsd,
        previousLocal,
        previousRate: prevRate,
        changeUsd: Math.round((usd - previousUsd) * 100) / 100,
        changePercent: Math.round((1000 * (usd - previousUsd)) / previousUsd) / 10,
        usageEffect: usage,
        fxEffect: fx,
        creditsUsd: -25,
      },
      drivers: [
        { serviceName: 'Amazon Elastic Compute Cloud', usd: 612.3, previousUsd: 668.9, changeUsd: -56.6 },
        { serviceName: 'Amazon Virtual Private Cloud', usd: 88.1, previousUsd: 61.4, changeUsd: 26.7 },
        { serviceName: 'Amazon Relational Database Service', usd: 214.5, previousUsd: 230.2, changeUsd: -15.7 },
        { serviceName: 'Amazon Simple Storage Service', usd: 88.3, previousUsd: 81.0, changeUsd: 7.3 },
      ],
      whatChanged:
        'Amazon Elastic Compute Cloud in eu-west-1 fell USD 56.60 (-8%) mainly due to EU-BoxUsage:m5.xlarge.',
      budgets: (await this.listBudgets(tenantId)).map((b) => ({
        name: b.name,
        currency: b.currency,
        amount: b.amount,
        status: b.status,
        actual: b.actual,
        actualPercent: b.actualPercent,
      })),
      savings: {
        openCount: recs.length,
        openUsdMonth: Math.round(recs.reduce((t, r) => t + r.savingsUsdMonth, 0) * 100) / 100,
        newCount: 2,
        doneCount: 1,
        doneUsdMonth: 18.4,
      },
      topRecommendations: recs.slice(0, 5).map((r) => ({
        title: r.title,
        savingsUsdMonth: r.savingsUsdMonth,
        riskLevel: r.riskLevel,
        effort: r.effort,
      })),
      anomalies: { count: 2, impactUsd: 189.5 },
      accuracy: {
        dataThrough: lastDay,
        reconciliation: 'ok',
        differencePercent: 0.2,
        checkedAt: `${lastDay}T08:00:00Z`,
        note:
          `From the AWS bills (CUR 2.0), amortised, after credits, up to ${lastDay}. The bills agree with Cost Explorer (within 1%).` +
          (rate === null ? '' : ` Converted at each day's official rate; month average ${tenant.displayCurrency} ${rate.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} per USD.`),
      },
    }
  }

  /** The demo estate burns about USD 18 a day of Activate credits. */
  async getCredits(tenantId: string): Promise<CreditsRunway> {
    await this.tick()
    this.tenant(tenantId)
    const empty: CreditsRunway = {
      status: 'no_credits', dataThrough: null, appliedMonthUsd: 0, burnUsdDay: 0, burnDays: 0,
      spendBeforeCreditsUsdMonth: 0, balance: null, usedSinceBalanceUsd: null, remainingUsd: null,
      daysLeft: null, depletionDate: null, expiresFirst: false, daily: [],
    }
    if (!this.hasData(tenantId)) return empty
    const last = isoDaysAgo(1)
    const daily = Array.from({ length: 30 }, (_, i) => ({ date: isoDaysAgo(30 - i), amount: Math.round((16 + ((i * 7) % 5)) * 100) / 100 }))
    const burn = Math.round((daily.reduce((t, d) => t + d.amount, 0) / 30) * 100) / 100
    const monthStart = `${last.slice(0, 8)}01`
    const applied = Math.round(daily.filter((d) => d.date >= monthStart).reduce((t, d) => t + d.amount, 0) * 100) / 100
    const entries = this.state.creditBalances?.[tenantId] ?? []
    const latest = entries[entries.length - 1]
    const base = { ...empty, dataThrough: last, appliedMonthUsd: applied, burnUsdDay: burn, burnDays: 30, spendBeforeCreditsUsdMonth: 1720.4, daily }
    if (!latest) return { ...base, status: 'no_balance' }
    const used = Math.round(daily.filter((d) => d.date > latest.asOf).reduce((t, d) => t + d.amount, 0) * 100) / 100
    const remaining = Math.max(Math.round((latest.amountUsd - used) * 100) / 100, 0)
    let daysLeft = Math.floor(remaining / burn)
    let depletion = new Date(Date.parse(`${last}T00:00:00Z`) + daysLeft * 86_400_000).toISOString().slice(0, 10)
    let expiresFirst = false
    if (latest.expiresOn && remaining > 0 && latest.expiresOn < depletion) {
      expiresFirst = true
      depletion = latest.expiresOn
      daysLeft = Math.max(Math.round((Date.parse(`${latest.expiresOn}T00:00:00Z`) - Date.parse(`${last}T00:00:00Z`)) / 86_400_000), 0)
    }
    return {
      ...base,
      status: remaining <= 0 ? 'used_up' : daysLeft < 90 ? 'ending_soon' : 'ok',
      balance: {
        amountUsd: latest.amountUsd,
        asOf: latest.asOf,
        expiresOn: latest.expiresOn ?? null,
        note: latest.note ?? null,
        enteredBy: this.state.me?.displayName ?? this.state.me?.email ?? null,
        enteredAt: latest.enteredAt,
      },
      usedSinceBalanceUsd: used,
      remainingUsd: remaining,
      daysLeft,
      depletionDate: depletion,
      expiresFirst,
    }
  }

  async setCreditBalance(tenantId: string, body: CreditBalanceInput): Promise<CreditsRunway> {
    this.tenant(tenantId)
    const today = isoDaysAgo(0)
    const asOf = body.asOf || today
    if (!(body.amountUsd >= 0)) throw new ApiError(422, 'The balance cannot be negative.')
    if (asOf > today) throw new ApiError(422, 'The balance date cannot be in the future.')
    if (body.expiresOn && body.expiresOn < asOf) throw new ApiError(422, 'Credits cannot expire before the balance date.')
    ;((this.state.creditBalances ??= {})[tenantId] ??= []).push({
      ...body,
      asOf,
      note: body.note?.trim() || null,
      enteredAt: new Date().toISOString(),
    })
    this.save()
    return this.getCredits(tenantId)
  }

  async listReports(tenantId: string): Promise<ReportList> {
    await this.tick()
    this.tenant(tenantId)
    const made = this.state.reportsMade?.[tenantId] ?? {}
    const reports = await Promise.all(
      Object.keys(made)
        .sort()
        .reverse()
        .map(async (month) => {
          const r = await this.demoReport(tenantId, month)
          return { month, generatedAt: made[month]!, spendUsd: r.spend.usd, spendLocal: r.spend.local, currency: r.currency }
        }),
    )
    return { reports, latestMonth: this.lastFullMonth() }
  }

  async getReport(tenantId: string, month: string): Promise<{ report: MonthlyReport; stored: boolean }> {
    await this.tick()
    this.tenant(tenantId)
    if (month >= new Date().toISOString().slice(0, 7)) throw new ApiError(422, 'A report covers a month that has ended.')
    if (!this.hasData(tenantId)) throw new ApiError(404, 'No bills for this month')
    return { report: await this.demoReport(tenantId, month), stored: Boolean(this.state.reportsMade?.[tenantId]?.[month]) }
  }

  async sendReport(tenantId: string, month: string): Promise<{ sentTo: number; report: MonthlyReport }> {
    await this.getReport(tenantId, month)
    ;((this.state.reportsMade ??= {})[tenantId] ??= {})[month] = new Date().toISOString()
    this.save()
    const prefs = await this.getNotificationPreferences(tenantId)
    const extra = this.state.reportEmails?.[tenantId] ?? []
    return { sentTo: (prefs.mine.monthlyReport ? 1 : 0) + extra.length, report: await this.demoReport(tenantId, month) }
  }

  async getReportSettings(tenantId: string): Promise<{ emails: string[] }> {
    await this.tick()
    this.tenant(tenantId)
    return { emails: [...(this.state.reportEmails?.[tenantId] ?? [])] }
  }

  async saveReportSettings(tenantId: string, emails: string[]): Promise<{ emails: string[] }> {
    this.tenant(tenantId)
    const clean = [...new Set(emails.map((e) => e.trim().toLowerCase()).filter(Boolean))].sort()
    if (clean.some((e) => !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e))) throw new ApiError(422, 'Enter valid email addresses.')
    ;(this.state.reportEmails ??= {})[tenantId] = clean
    this.save()
    return { emails: clean }
  }

  async getAlertSettings(tenantId: string): Promise<AlertSettingsView> {
    await this.tick()
    this.tenant(tenantId)
    const settings = structuredClone(this.state.alertSettings?.[tenantId] ?? DEFAULT_ALERT_SETTINGS)
    const rate = settings.anomalyMinImpactCurrency === 'USD' ? 1 : ((await this.fxCurrent(tenantId)).quote?.rate ?? null)
    return {
      settings,
      minImpactUsd: rate ? Math.round((settings.anomalyMinImpact / rate) * 100) / 100 : null,
      defaults: structuredClone(DEFAULT_ALERT_SETTINGS),
    }
  }

  async saveAlertSettings(tenantId: string, settings: AlertSettings): Promise<AlertSettingsView> {
    const local = this.tenant(tenantId).displayCurrency
    if (![local, 'USD'].includes(settings.anomalyMinImpactCurrency))
      throw new ApiError(422, `The minimum is in USD or the workspace currency (${local}).`)
    if (!(settings.anomalyMinImpact >= 1)) throw new ApiError(422, 'The minimum must be at least 1.')
    ;(this.state.alertSettings ??= {})[tenantId] = {
      ...structuredClone(settings),
      anomalyEmails: [...new Set(settings.anomalyEmails.map((e) => e.trim().toLowerCase()).filter(Boolean))].sort(),
    }
    this.save()
    return this.getAlertSettings(tenantId)
  }

  async getRuleSettings(tenantId: string): Promise<RuleSettingsView> {
    await this.tick()
    this.tenant(tenantId)
    return { rules: DEMO_RULES, settings: structuredClone(this.ruleSettings(tenantId)) }
  }

  async saveRuleSettings(tenantId: string, settings: RuleSettings): Promise<RuleSettingsView> {
    this.tenant(tenantId)
    ;(this.state.ruleSettings ??= {})[tenantId] = structuredClone(settings)
    this.save()
    return this.getRuleSettings(tenantId)
  }

  // The demo has no stored audit trail: it lists what this browser's state implies.
  async auditLog(tenantId: string): Promise<AuditPage> {
    await this.tick()
    const tenant = this.tenant(tenantId)
    const me = this.requireMe()
    const actor = { type: 'user' as const, email: me.email, name: me.displayName }
    const entries: Omit<AuditEntry, 'id'>[] = [
      {
        occurredAt: tenant.createdAt,
        actor,
        action: 'tenant.created',
        targetType: 'tenant',
        targetId: tenant.id,
        before: null,
        after: { name: tenant.name, countryCode: tenant.countryCode },
        ip: null,
      },
    ]
    for (const c of this.state.connections[tenantId] ?? []) {
      entries.push({
        occurredAt: c.createdAt,
        actor,
        action: 'connection.created',
        targetType: 'cloud_connection',
        targetId: c.id,
        before: null,
        after: { awsAccountId: c.awsAccountId, accountType: c.accountType },
        ip: null,
      })
      if (c.lastValidatedAt && c.status !== 'pending') {
        entries.push({
          occurredAt: c.lastValidatedAt,
          actor,
          action: 'connection.status_changed',
          targetType: 'cloud_connection',
          targetId: c.id,
          before: { status: 'pending' },
          after: { status: c.status },
          ip: null,
        })
      }
    }
    entries.sort((a, b) => b.occurredAt.localeCompare(a.occurredAt))
    return { entries: entries.map((e, i) => ({ ...e, id: entries.length - i })), nextBefore: null, verified: true }
  }

  /** The first connection with data stands in as the payer. */
  private payer(tenantId: string): Connection | undefined {
    return (this.state.connections[tenantId] ?? []).find((c) => c.status === 'healthy' || c.status === 'degraded')
  }

  private advisory(tenantId: string, payer: Connection): SavingsPlanAdvisory {
    const currency = this.tenant(tenantId).displayCurrency
    const rate = currency === 'USD' ? 1 : (DEMO_RATES[currency] ?? null)
    return demoAdvisory(payer.awsAccountId, payer.id, currency, rate)
  }

  async listCommitments(tenantId: string): Promise<CommitmentList> {
    await this.tick()
    this.tenant(tenantId)
    const payer = this.payer(tenantId)
    return payer ? demoCommitments(payer.awsAccountId, payer.id) : { commitments: [], coverage: [], expiryAlertDays: [60, 30, 15] }
  }

  async savingsPlanAdvisory(tenantId: string): Promise<SavingsPlanAdvisory> {
    await this.tick()
    this.tenant(tenantId)
    const payer = this.payer(tenantId)
    if (!payer) return { advice: [], returnWindowDays: 7, ladderFirstPercent: 25, ladderStepPercent: 10, fxStressPercent: [20, 40] }
    return this.advisory(tenantId, payer)
  }

  async refreshCommitments(tenantId: string): Promise<{ status: string; connections: number }> {
    await this.tick()
    this.tenant(tenantId)
    const payer = this.payer(tenantId)
    return payer ? { status: 'succeeded', connections: 1 } : { status: 'skipped', connections: 0 }
  }

  private hasData(tenantId: string): boolean {
    return (this.state.connections[tenantId] ?? []).some((c) => c.status === 'healthy' || c.status === 'degraded')
  }

  private connection(tenantId: string, connectionId: string): Connection {
    this.tenant(tenantId)
    const conn = (this.state.connections[tenantId] ?? []).find((c) => c.id === connectionId)
    if (!conn) throw new ApiError(404, 'Connection not found')
    return conn
  }

  private setup(conn: Connection): ConnectionSetup {
    const base = `${import.meta.env.BASE_URL}cfn`
    return {
      connection: structuredClone(conn),
      externalId: this.state.externalIds[conn.id] ?? '',
      // The demo has no S3-hosted template, so it shows the download path, like staging today.
      quickCreateUrl: null,
      templateUrl: `${base}/billing-role.yaml`,
      templateDownloadUrl: `${base}/billing-role.yaml`,
      resourceTemplateUrl: `${base}/resource-role.yaml`,
      resourceTemplateDownloadUrl: `${base}/resource-role.yaml`,
      resourceQuickCreateUrl: null,
      resourceStacksetQuickCreateUrl: null,
      resourceStacksetDownloadUrl: `${base}/resource-stackset.yaml`,
      stackRegion: 'us-east-1',
      principalArn: 'arn:aws:iam::000000000000:role/CloudopticsBroker',
      accessQuickCreateUrl: null,
      accessTemplateUrl: `${base}/cloudoptics-access.yaml`,
      accessTemplateDownloadUrl: `${base}/cloudoptics-access.yaml`,
      callbackUrl: 'https://cloudoptics.example/api/v1/cfn/callback',
      connectionRef: `c:${conn.id}`,
      latestStackVersion: '2.0.3',
      updateAvailable: conn.stackName !== 'cloudoptics-access' || conn.stackVersion !== '2.0.3',
      stackConsoleUrl:
        'https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/stackinfo?stackId=' +
        (conn.stackName ?? 'cloudoptics-billing-access'),
      stackUpdateUrl:
        'https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/update/template?stackId=cloudoptics-access&templateURL=' +
        encodeURIComponent(`${base}/cloudoptics-access.yaml`),
    }
  }
}
