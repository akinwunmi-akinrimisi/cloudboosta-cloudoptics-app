// The FAQ shared by the landing page, /how-it-works and /security (docs/14 §5.3). Answers are
// plain text so the FAQPage JSON-LD says exactly what the page shows. Times, names and limits
// come from the claims file and the generated permission data, never typed here twice.
import { billingRole, claims, facts, resourceRole, sessionHours } from '../config'

export interface FaqItem {
  id: string
  q: string
  a: string[]
}

const hours = sessionHours === 1 ? 'one hour' : `${sessionHours} hours`

export const FAQ: Record<string, FaqItem> = {
  change: {
    id: 'can-it-change-anything',
    q: 'Can Cloudoptics change anything in my account?',
    a: [
      `No. The two roles Cloudoptics uses, ${billingRole.roleName} and ${resourceRole.roleName}, allow only read actions (Get, Describe, List and View). Our build checks the template for this and fails if a write action appears.`,
      'The stack also has a setup step that creates the bill-export bucket and rolls out member-account access. It runs in your account while CloudFormation creates, updates or deletes the stack, and Cloudoptics cannot assume its role.',
    ],
  },
  files: {
    id: 'can-it-read-my-files',
    q: 'Can it read my files or databases?',
    a: [
      'No. It reads bill files from the one bucket the stack creates, plus resource settings, tags and usage metrics.',
      'It cannot read files in your other buckets, database contents, secrets, Systems Manager parameters, log contents, Lambda configuration or instance user data.',
    ],
  },
  keys: {
    id: 'do-you-store-aws-keys',
    q: 'Do you store AWS keys?',
    a: [
      `No, and you never give us any. Cloudoptics assumes the read-only roles for up to ${hours} at a time, and only with the ExternalId that is unique to your workspace.`,
    ],
  },
  data: {
    id: 'where-is-my-data',
    q: 'Where is my data stored?',
    a: [
      `Cloudoptics runs on ${claims.hosting}. Your browser and AWS talk to it over HTTPS.`,
      'How long we keep data after you delete a workspace will be set out in our privacy notice, which is being prepared.',
    ],
  },
  time: {
    id: 'how-long-does-setup-take',
    q: 'How long does setup take?',
    a: [
      `About ${claims.setupMinutes} minutes, and about ${claims.awsMinutes} of them in AWS. Most of the rest is waiting for AWS to finish creating the stack.`,
    ],
  },
  results: {
    id: 'when-do-i-see-results',
    q: 'When do I see results?',
    a: [`Recommendations appear ${claims.firstRecommendations}. Costs appear ${claims.firstCosts}.`],
  },
  orgs: {
    id: 'aws-organizations',
    q: 'Does it work with AWS Organizations?',
    a: [
      `Yes. Create one stack in the management account and it gives every member account the resource role through a StackSet (${facts.stackSetName}), including accounts you add later. This needs AWS Organizations with all features turned on.`,
    ],
  },
  cost: {
    id: 'what-does-it-cost',
    q: 'What does it cost?',
    a: [
      `${claims.pricing.headline}. ${claims.pricing.after}`,
      `In AWS you pay for storing the bill export in your own S3 bucket and for a small number of paid API calls, such as Cost Explorer at USD ${claims.costExplorerRequestUsd.toFixed(2)} a request.`,
    ],
  },
  remove: {
    id: 'how-do-i-remove-it',
    q: 'How do I remove it?',
    a: [
      `Delete the ${claims.stackName} stack in CloudFormation (${facts.region}). That removes both roles, the bill export and, in an Organization, the member-account roles.`,
      `The bucket ${facts.bucketName} and the bills in it are kept on purpose, so you keep your bill history. Delete the bucket yourself if you want them gone.`,
    ],
  },
  aws: {
    id: 'is-it-an-aws-product',
    q: 'Is Cloudoptics an AWS product?',
    a: ['No. Cloudoptics is built by Cloudboosta and is not affiliated with or endorsed by Amazon Web Services.'],
  },
}

export const LANDING_FAQ = [FAQ.change, FAQ.files, FAQ.keys, FAQ.data, FAQ.time, FAQ.results, FAQ.orgs, FAQ.cost, FAQ.remove, FAQ.aws]
export const HOW_FAQ = [FAQ.change, FAQ.files, FAQ.keys, FAQ.orgs, FAQ.remove]
export const SECURITY_FAQ = [FAQ.change, FAQ.files, FAQ.keys, FAQ.data, FAQ.remove]
