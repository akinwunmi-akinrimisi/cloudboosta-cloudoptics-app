// Canned Azure setup and access check for the demo (DemoApi). Nothing here talks to Azure.

import type { AzureConnection, AzureConnectionCreate, AzureConnectionSetup, PermissionCheck } from './types'

const DEMO_CLIENT_ID = '00000000-0000-4000-8000-00000000c0de'

export function demoAzureConnection(id: string, body: AzureConnectionCreate): AzureConnection {
  const scope =
    body.scopeType === 'billing_account'
      ? `/providers/Microsoft.Billing/billingAccounts/${body.billingAccountId ?? ''}`
      : `/subscriptions/${body.subscriptionIds[0] ?? ''}`
  return {
    id,
    provider: 'azure',
    status: 'pending',
    directoryId: body.directoryId,
    scopeType: body.scopeType,
    scope,
    subscriptionIds: [...body.subscriptionIds],
    billingAccountId: body.billingAccountId,
    storageAccount: body.storageAccount,
    container: body.container,
    exportPath: body.exportPath,
    permissionChecks: [],
    lastError: null,
    lastValidatedAt: null,
    createdAt: new Date().toISOString(),
  }
}

export function demoAzureSetup(conn: AzureConnection): AzureConnectionSetup {
  const storageScope = `/subscriptions/${conn.subscriptionIds[0] ?? '<subscription-id>'}/resourceGroups/<resource-group>/providers/Microsoft.Storage/storageAccounts/${conn.storageAccount}`
  return {
    connection: structuredClone(conn),
    clientId: DEMO_CLIENT_ID,
    consentUrl: `https://login.microsoftonline.com/${conn.directoryId}/adminconsent?client_id=${DEMO_CLIENT_ID}`,
    ownershipTag: 'cloudoptics-verify',
    ownershipCode: 'co-demo0000000000000000',
    roleAssignmentCommand: [
      `SP=$(az ad sp show --id ${DEMO_CLIENT_ID} --query id -o tsv)`,
      `az role assignment create --assignee-object-id "$SP" --assignee-principal-type ServicePrincipal \\`,
      `  --role "Reader" --scope "${conn.scope}"`,
      `az role assignment create --assignee-object-id "$SP" --assignee-principal-type ServicePrincipal \\`,
      `  --role "Cost Management Reader" --scope "${conn.scope}"`,
      `az role assignment create --assignee-object-id "$SP" --assignee-principal-type ServicePrincipal \\`,
      `  --role "Storage Blob Data Reader" --scope "${storageScope}/blobServices/default/containers/${conn.container}"`,
      `az tag update --resource-id "${storageScope}" --operation Merge --tags cloudoptics-verify=co-demo0000000000000000`,
    ].join('\n'),
    portalRoleSteps: [
      'In the Azure portal, open the subscription and choose Access control (IAM).',
      'Choose Add, then Add role assignment.',
      'Pick Reader, then Members, Select members, and search for Cloudoptics.',
      'Choose Review + assign. Repeat for Cost Management Reader.',
      `Open the storage account ${conn.storageAccount} and assign Storage Blob Data Reader the same way.`,
    ],
    exportPortalUrl: 'https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/exports',
    exportSteps: [
      'In Cost Management, choose Exports, then Create.',
      'Pick the template "Cost and usage (FOCUS)" and a daily export of month-to-date costs.',
      `Choose storage account ${conn.storageAccount}, container ${conn.container}${conn.exportPath ? ` and directory ${conn.exportPath}` : ''}.`,
      'Choose Parquet or CSV, then Review + create.',
    ],
  }
}

export const DEMO_AZURE_CHECKS: PermissionCheck[] = [
  { id: 'consent', label: 'Admin consent', status: 'ok', message: 'Cloudoptics can sign in to your directory.', hint: null },
  { id: 'reader', label: 'Reader role', status: 'ok', message: 'Resources readable.', hint: null },
  { id: 'cost_reader', label: 'Cost Management Reader role', status: 'ok', message: 'Cost data readable.', hint: null },
  { id: 'export', label: 'Find the FOCUS cost export', status: 'ok', message: 'Found a daily FOCUS export.', hint: null },
  {
    id: 'ownership',
    label: 'Prove the storage account is yours',
    status: 'ok',
    message: 'The storage account carries your code.',
    hint: null,
  },
  { id: 'blob_reader', label: 'Read the export container', status: 'ok', message: 'Container readable.', hint: null },
  {
    id: 'export_data',
    label: 'First export file',
    status: 'warning',
    message: 'No export file yet; the first run usually arrives within a day.',
    hint: 'We will check again automatically.',
  },
]
