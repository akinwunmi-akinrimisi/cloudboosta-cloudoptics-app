import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { NatRoutes } from './natRoutes'

const region = 'af-south-1'

describe('NatRoutes', () => {
  it('lists each route table still pointing at the gateway with a console link and commands', () => {
    render(
      <NatRoutes
        routes={{
          natGatewayId: 'nat-1',
          tables: [
            {
              routeTableId: 'rtb-private',
              vpcId: 'vpc-1',
              destinations: ['0.0.0.0/0'],
              subnetIds: ['subnet-a', 'subnet-b'],
              main: false,
              consoleUrl: `https://${region}.console.aws.amazon.com/vpc/home?region=${region}#RouteTableDetails:RouteTableId=rtb-private`,
              inspect: `aws ec2 describe-route-tables --route-table-ids rtb-private --region ${region}`,
              replace: [
                `aws ec2 replace-route --route-table-id rtb-private --destination-cidr-block 0.0.0.0/0 --nat-gateway-id '<other-nat-gateway-id>' --region ${region}`,
              ],
              delete: [
                `aws ec2 delete-route --route-table-id rtb-private --destination-cidr-block 0.0.0.0/0 --region ${region}`,
              ],
            },
            {
              routeTableId: 'rtb-main',
              vpcId: 'vpc-1',
              destinations: ['pl-1'],
              subnetIds: [],
              main: true,
              consoleUrl: 'https://example.test/rtb-main',
              inspect: 'aws ec2 describe-route-tables --route-table-ids rtb-main',
              replace: [],
              delete: [],
            },
          ],
        }}
      />,
    )
    expect(screen.getByRole('heading', { name: '2 route tables still send traffic to it' })).toBeTruthy()
    expect(screen.getByRole('link', { name: /rtb-private/ }).getAttribute('href')).toContain(
      '#RouteTableDetails:RouteTableId=rtb-private',
    )
    expect(screen.getByText(/Sends 0\.0\.0\.0\/0 to the gateway, for subnets subnet-a, subnet-b/)).toBeTruthy()
    expect(screen.getByText(/for every subnet without its own route table/)).toBeTruthy()
    expect(screen.getByText('Main route table')).toBeTruthy()
    expect(screen.getByText(/aws ec2 replace-route --route-table-id rtb-private/)).toBeTruthy()
    expect(screen.getByText(/Cloudoptics never changes routes/)).toBeTruthy()
    expect(screen.getAllByText('If these subnets need no internet access')).toHaveLength(1)
  })
})
