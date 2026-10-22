import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
import { CreCfnParameter } from '../core/cre-cfn-parameter.js';
import { CreStack, CreStackProps } from '../core/cre-stack.js';
import { PeeringMesh } from './peering-mesh.js';
import { IpMode } from './parameterized-vpc.js';
import { RegionalNetwork, RegionalNetworkProps, ScopedIngress } from './regional-network.js';

export interface CreNetworkStackProps extends CreStackProps {
  /** The VPC CIDR: the `VpcCidr` parameter's default. @default 10.0.0.0/16 */
  readonly vpcCidr?: string;
  /** Everything but the CIDR. */
  readonly network?: Omit<RegionalNetworkProps, 'vpcCidr'>;
}

/** Props of the two standard network stacks: the topology is fixed, the rest is yours. */
export interface CreStandardNetworkStackProps extends CreStackProps {
  /** The VPC CIDR: the `VpcCidr` parameter's default. @default 10.0.0.0/16 */
  readonly vpcCidr?: string;
  /** IPv4, dual-stack or IPv6-only. Default `ipv4`. */
  readonly ipMode?: IpMode;
  /** AZ count. Default 3. */
  readonly azCount?: number;
  /** Interface endpoints. Default [] (each costs money). */
  readonly interfaceEndpoints?: ec2.InterfaceVpcEndpointAwsService[];
  /** Scope a named interface endpoint's SG to a CIDR. */
  readonly scopedIngress?: ScopedIngress[];
}

/**
 * A stack that owns one region's network, one per region (e.g. `<project>-network-us-east-1`).
 * Environment-agnostic like every CreStack: its `region` prop says where it deploys.
 *
 *   - A {@link RegionalNetwork} whose CIDR is the `VpcCidr` parameter (default the `vpcCidr` prop).
 *   - Outputs `VpcId`, `VpcCidr`, `RouteTableIds` (comma-joined), `Region`, and `VpcIpv6Cidr` when
 *     the network has IPv6. {@link CrePeeringStack} takes the first four (CreCfnParameters). Peering
 *     is IPv4-only, so it does nothing for an `ipv6` network.
 *
 * Add the workload to `network.vpc`, in a subclass or on the instance. Prefer
 * {@link CrePrivateNetworkStack} or {@link CreEgressOnlyNetworkStack}; use this with `network` for a
 * custom topology.
 */
export class CreNetworkStack extends CreStack {
  public readonly network: RegionalNetwork;
  /** The values of `VpcId`, `VpcCidr`, `RouteTableIds` and `Region`, as output. */
  public readonly peeringValues: { vpcId: string; vpcCidr: string; routeTableIds: string; region: string };

  constructor(
    scope: Construct,
    id: string,
    props: CreNetworkStackProps,
    build: (stack: CreNetworkStack, vpcCidr: string) => RegionalNetwork =
    (stack, vpcCidr) => new RegionalNetwork(stack, 'Network', { ...(props.network ?? {}), vpcCidr }),
  ) {
    super(scope, id, props);
    const vpcCidr = new cdk.CfnParameter(this, 'VpcCidr', { type: 'String', default: props.vpcCidr ?? '10.0.0.0/16' });
    this.network = build(this, vpcCidr.valueAsString);
    this.peeringValues = {
      vpcId: this.network.vpc.vpcId,
      vpcCidr: vpcCidr.valueAsString,
      routeTableIds: cdk.Fn.join(',', this.network.peeringRouteTableIds),
      region: Aws.REGION,
    };
    new cdk.CfnOutput(this, 'VpcId', { value: this.peeringValues.vpcId });
    // Same logical id as the parameter (Outputs are their own namespace in a template).
    new cdk.CfnOutput(this, 'VpcCidrOutput', { value: this.peeringValues.vpcCidr }).overrideLogicalId('VpcCidr');
    if (this.network.vpcIpv6CidrBlock) {
      new cdk.CfnOutput(this, 'VpcIpv6Cidr', { value: this.network.vpcIpv6CidrBlock });
    }
    new cdk.CfnOutput(this, 'RouteTableIds', { value: this.peeringValues.routeTableIds });
    new cdk.CfnOutput(this, 'Region', { value: this.peeringValues.region });
  }
}

/**
 * A {@link CreNetworkStack} with the private network ({@link RegionalNetwork.privateNetwork}):
 * isolated subnets only and an attached, unrouted internet gateway. Nothing reaches the internet.
 * Where Locust tasks and internal ALBs behind the edge live; give it the interface endpoints the
 * workload needs (ECR, ECR_DOCKER and CLOUDWATCH_LOGS for Fargate).
 */
export class CrePrivateNetworkStack extends CreNetworkStack {
  constructor(scope: Construct, id: string, props: CreStandardNetworkStackProps) {
    super(scope, id, props, (stack, vpcCidr) => RegionalNetwork.privateNetwork(stack, 'Network', { ...networkProps(props), vpcCidr }));
  }
}

/**
 * A {@link CreNetworkStack} with the egress-only network ({@link RegionalNetwork.egressOnly}):
 * private subnets with outbound internet through one NAT gateway per AZ (IPv4) and an egress-only
 * internet gateway (IPv6). For workloads that call the internet. NAT gateways bill per hour and
 * per GB: three AZs is three NATs.
 */
export class CreEgressOnlyNetworkStack extends CreNetworkStack {
  constructor(scope: Construct, id: string, props: CreStandardNetworkStackProps) {
    super(scope, id, props, (stack, vpcCidr) => RegionalNetwork.egressOnly(stack, 'Network', { ...networkProps(props), vpcCidr }));
  }
}

function networkProps(props: CreStandardNetworkStackProps) {
  return {
    ipMode: props.ipMode,
    azCount: props.azCount,
    interfaceEndpoints: props.interfaceEndpoints,
    scopedIngress: props.scopedIngress,
  };
}

export interface CrePeeringStackProps extends CreStackProps {
  /** The network stacks it peers, one per region, at least 2. */
  readonly networks: CreNetworkStack[];
}

/**
 * The cross-region peering mesh. Takes `R<i>Region`, `R<i>VpcId`, `R<i>VpcCidr` and
 * `R<i>RouteTableIds` from each {@link CreNetworkStack}'s outputs (CreCfnParameters), so it
 * deploys after them, and peers every pair with {@link PeeringMesh}.
 */
export class CrePeeringStack extends CreStack {
  public readonly mesh: PeeringMesh;

  constructor(scope: Construct, id: string, props: CrePeeringStackProps) {
    super(scope, id, props);
    if (props.networks.length < 2) throw new Error('CrePeeringStack: give at least 2 network stacks.');
    const p = (name: string, net: CreNetworkStack, value: string, output: string) =>
      new CreCfnParameter(this, name, { from: { stack: net, value, outputName: output } }).valueAsString;
    const peers = props.networks.map((net, i) => ({
      region: p(`R${i}Region`, net, net.peeringValues.region, 'Region'),
      vpcId: p(`R${i}VpcId`, net, net.peeringValues.vpcId, 'VpcId'),
      vpcCidr: p(`R${i}VpcCidr`, net, net.peeringValues.vpcCidr, 'VpcCidr'),
      routeTableIds: cdk.Fn.split(',', p(`R${i}RouteTableIds`, net, net.peeringValues.routeTableIds, 'RouteTableIds')),
    }));
    this.mesh = new PeeringMesh(this, 'Mesh', { peers });
  }
}
