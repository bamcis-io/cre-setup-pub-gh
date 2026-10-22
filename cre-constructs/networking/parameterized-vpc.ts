import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

/**
 * A VPC whose CIDR and AZs are chosen at deploy time. The deploy-time CIDR
 * (Fn::Cidr) + deploy-time AZ selection (Fn::GetAZs/Fn::Select) approach is what
 * lets a single template deploy to N regions with different CfnParameter CIDRs.
 */

/**
 * The IP addressing of a whole network.
 *   - `ipv4`: IPv4 only.
 *   - `dualstack`: every subnet gets an IPv4 CIDR and an IPv6 /64 from an Amazon-provided /56;
 *     ENIs get both. IPv4 egress via NAT, IPv6 egress via the egress-only internet gateway.
 *   - `ipv6`: IPv6-only (IPv6-native) subnets. The VPC still has an IPv4 CIDR (AWS requires one),
 *     but no subnet uses it. Egress via the egress-only internet gateway only; there is no NAT64,
 *     so IPv4-only internet destinations are unreachable (AWS service endpoints that offer IPv6
 *     work). Not everything runs in IPv6-only subnets: see the README.
 */
export type IpMode = 'ipv4' | 'dualstack' | 'ipv6';

export interface SubnetSpec {
  /** Logical prefix for the subnet construct IDs (e.g. 'Public', 'Private'). */
  readonly name: string;
  /** Subnet kind (affects route table defaults + downstream consumers). */
  readonly subnetType: ec2.SubnetType;
  /** Whether to associate a PublicIpOnLaunch flag. Derived from subnetType if omitted. */
  readonly mapPublicIpOnLaunch?: boolean;
}

export interface ParameterizedVpcProps {
  /** VPC CIDR. May be a CfnParameter token. */
  readonly vpcCidr: string;
  /**
   * Ordered list of subnet tiers. One subnet per AZ per tier is produced,
   * carved from the VPC CIDR using `Fn::Cidr` so subnet blocks are assigned
   * deterministically from the top of the VPC CIDR.
   *
   * Example: 3 AZs × [Public, Private] → 6 subnets.
   */
  readonly subnets: SubnetSpec[];
  /** Number of /24 bits to carve from the VPC CIDR for each subnet. Default 8. */
  readonly cidrMaskBits?: number;
  /** Number of AZs to use. Default 3. AZ names come from `Fn::GetAZs`. */
  readonly azCount?: number;
  /**
   * One NAT gateway per AZ, in that AZ's public subnet, each private-with-egress subnet routing
   * IPv4 to its own AZ's NAT (no cross-AZ dependency). Ignored in
   * `ipv6` mode (no IPv4 egress). Default false.
   */
  readonly natGatewayPerAz?: boolean;
  /** IPv4, dual-stack or IPv6-only. Default `ipv4`. */
  readonly ipMode?: IpMode;
  /**
   * If true, attach an Internet Gateway to the VPC even when there are
   * no public subnets. CloudFront VPC origins require the VPC to have
   * an IGW attached — CloudFront checks the VPC's "can receive traffic
   * from the internet" flag, not the subnet routing tables. The IGW is
   * otherwise unused in this case; traffic to/from VPC origins goes
   * through CloudFront's managed ENIs, not through the IGW.
   *
   * Implied true when any subnet tier is PUBLIC.
   */
  readonly includeInternetGateway?: boolean;
}

/**
 * A VPC whose CIDR is supplied at deploy time via a CfnParameter. The L2
 * `ec2.Vpc` construct requires a literal CIDR (it subdivides subnets during
 * synth), so this helper builds `CfnVPC` + `CfnSubnet` directly and exposes
 * an `ec2.IVpc` for downstream consumers.
 *
 * Subnets are carved from the VPC CIDR via `Fn::Cidr`, so changing the
 * parameter at deploy time automatically shifts the subnet ranges.
 */
export class ParameterizedVpc extends Construct {
  public readonly vpc: ec2.IVpc;
  public readonly vpcId: string;
  public readonly vpcCidrBlock: string;
  public readonly publicSubnets: ec2.ISubnet[] = [];
  public readonly privateSubnets: ec2.ISubnet[] = [];
  public readonly isolatedSubnets: ec2.ISubnet[] = [];
  public readonly internetGatewayId?: string;
  public readonly ipMode: IpMode;
  /** The VPC's Amazon-provided IPv6 /56 (`dualstack` and `ipv6` modes). */
  public readonly vpcIpv6CidrBlock?: string;
  /** The egress-only internet gateway (`dualstack`/`ipv6` with private-with-egress subnets). */
  public readonly egressOnlyInternetGatewayId?: string;
  /** NAT gateway ids, one per AZ (`natGatewayPerAz`), in AZ order. */
  public readonly natGatewayIds: string[] = [];

  private readonly allSubnets: ec2.ISubnet[] = [];

  constructor(scope: Construct, id: string, props: ParameterizedVpcProps) {
    super(scope, id);

    const azCount = props.azCount ?? 3;
    const cidrMaskBits = props.cidrMaskBits ?? 8;
    // AZ names are resolved at deploy time via Fn::GetAZs. We don't want
    // to hardcode letter suffixes — ca-central-1 skips 'c' and uses 'd',
    // and other partitions/regions have their own quirks. Fn::GetAZs
    // returns the actual list of AZ names for the region the stack is
    // deployed in, which we then Select from by index.
    const azList = cdk.Fn.getAzs(Aws.REGION);
    const azs = Array.from({ length: azCount }, (_, i) => cdk.Fn.select(i, azList));

    const ipMode: IpMode = props.ipMode ?? 'ipv4';
    this.ipMode = ipMode;
    const hasV6 = ipMode !== 'ipv4';
    const hasV4Subnets = ipMode !== 'ipv6';

    // --- VPC resource -------------------------------------------------------
    // A VPC always has an IPv4 CIDR, even when every subnet is IPv6-native.
    const cfnVpc = new ec2.CfnVPC(this, 'Resource', {
      cidrBlock: props.vpcCidr,
      enableDnsHostnames: true,
      enableDnsSupport: true,
    });
    this.vpcId = cfnVpc.ref;
    this.vpcCidrBlock = props.vpcCidr;

    // Amazon-provided /56; subnets take /64s from it. Every subnet depends on the association,
    // because Fn::GetAtt VPC.Ipv6CidrBlocks is empty until it exists.
    let v6Block: ec2.CfnVPCCidrBlock | undefined;
    let v6SubnetCidrs: string[] | undefined;
    if (hasV6) {
      v6Block = new ec2.CfnVPCCidrBlock(this, 'Ipv6CidrBlock', {
        vpcId: this.vpcId,
        amazonProvidedIpv6CidrBlock: true,
      });
      this.vpcIpv6CidrBlock = cdk.Fn.select(0, cfnVpc.attrIpv6CidrBlocks);
      // Fn::Cidr's last argument is the number of HOST bits: 64 → /64 subnets.
      v6SubnetCidrs = cdk.Fn.cidr(this.vpcIpv6CidrBlock, props.subnets.length * (props.azCount ?? 3), '64');
    }
    const hasEgressTier = props.subnets.some((s) =>
      s.subnetType === ec2.SubnetType.PRIVATE_WITH_EGRESS || s.subnetType === ec2.SubnetType.PRIVATE_WITH_NAT);
    let eigw: ec2.CfnEgressOnlyInternetGateway | undefined;
    if (hasV6 && hasEgressTier) {
      eigw = new ec2.CfnEgressOnlyInternetGateway(this, 'EgressOnlyIGW', { vpcId: this.vpcId });
      this.egressOnlyInternetGatewayId = eigw.attrId;
    }

    // --- Internet gateway ----------------------------------------------------
    // An IGW is created when:
    //   - the VPC has PUBLIC subnets (routed via 0.0.0.0/0 → IGW), OR
    //   - the caller explicitly asks for one via includeInternetGateway.
    //     CloudFront VPC origins require an IGW attachment on the VPC
    //     even though traffic never flows through it — it's a static
    //     "this VPC can face the internet" flag that CloudFront checks.
    const hasPublic = props.subnets.some((s) => s.subnetType === ec2.SubnetType.PUBLIC);
    const needsIgw = hasPublic || (props.includeInternetGateway ?? false);
    let igw: ec2.CfnInternetGateway | undefined;
    // Anything that routes through the IGW (public default routes, the NAT EIP and
    // gateway) must wait for the ATTACHMENT, not just the IGW: referencing igw.ref only
    // orders after the gateway exists, and CloudFormation can then create the route or
    // EIP before it is attached to the VPC — an intermittent first-deploy failure. The
    // L2 ec2.Vpc adds the same dependency; the AWS::EC2::EIP docs require it.
    let igwAttachment: ec2.CfnVPCGatewayAttachment | undefined;
    if (needsIgw) {
      igw = new ec2.CfnInternetGateway(this, 'IGW', {});
      igwAttachment = new ec2.CfnVPCGatewayAttachment(this, 'IGWAttachment', {
        vpcId: this.vpcId,
        internetGatewayId: igw.ref,
      });
      this.internetGatewayId = igw.ref;
    }

    // --- Subnets ------------------------------------------------------------
    // `Fn::Cidr(vpcCidr, count, cidrBits)` → list of CIDR strings to carve.
    const totalSubnets = props.subnets.length * azCount;
    const subnetCidrs = cdk.Fn.cidr(props.vpcCidr, totalSubnets, cidrMaskBits.toString());

    // Public subnet per AZ index (NAT gateways live there).
    const publicSubnetByAz: (string | undefined)[] = [];

    const egressRtbByAz: { azIdx: number; rtb: ec2.CfnRouteTable }[] = [];
    let subnetIndex = 0;
    for (const spec of props.subnets) {
      const bucket = this.bucketFor(spec.subnetType);

      for (let azIdx = 0; azIdx < azCount; azIdx++) {
        const az = azs[azIdx];
        const subnetCidr = cdk.Fn.select(subnetIndex, subnetCidrs);
        const logicalId = `${spec.name}Subnet${azIdx + 1}`;

        const cfnSubnet = new ec2.CfnSubnet(this, logicalId, {
          vpcId: this.vpcId,
          availabilityZone: az,
          ...(hasV4Subnets
            ? {
              cidrBlock: subnetCidr,
              mapPublicIpOnLaunch: spec.mapPublicIpOnLaunch ?? spec.subnetType === ec2.SubnetType.PUBLIC,
            }
            : {}),
          ...(hasV6
            ? {
              ipv6CidrBlock: cdk.Fn.select(subnetIndex, v6SubnetCidrs!),
              assignIpv6AddressOnCreation: true,
            }
            : {}),
          ...(ipMode === 'ipv6'
            ? {
              ipv6Native: true,
              // IPv6-native subnets can't use IP-based hostnames.
              privateDnsNameOptionsOnLaunch: { HostnameType: 'resource-name', EnableResourceNameDnsAAAARecord: true },
            }
            : {}),
        });
        if (v6Block) cfnSubnet.addDependency(v6Block);

        const rtb = new ec2.CfnRouteTable(this, `${logicalId}RouteTable`, {
          vpcId: this.vpcId,
        });

        new ec2.CfnSubnetRouteTableAssociation(this, `${logicalId}RouteTableAssociation`, {
          subnetId: cfnSubnet.ref,
          routeTableId: rtb.ref,
        });

        // Default routes per subnet type. Isolated subnets get none (an attached IGW, e.g. for
        // CloudFront VPC origins, is never routed to from them).
        if (spec.subnetType === ec2.SubnetType.PUBLIC && igw) {
          if (hasV4Subnets) {
            const defaultRoute = new ec2.CfnRoute(this, `${logicalId}DefaultRoute`, {
              routeTableId: rtb.ref,
              destinationCidrBlock: '0.0.0.0/0',
              gatewayId: igw.ref,
            });
            defaultRoute.addDependency(igwAttachment!);
          }
          if (hasV6) {
            const v6Route = new ec2.CfnRoute(this, `${logicalId}DefaultRouteV6`, {
              routeTableId: rtb.ref,
              destinationIpv6CidrBlock: '::/0',
              gatewayId: igw.ref,
            });
            v6Route.addDependency(igwAttachment!);
          }
          publicSubnetByAz[azIdx] ??= cfnSubnet.ref;
        }
        const isEgress = spec.subnetType === ec2.SubnetType.PRIVATE_WITH_EGRESS || spec.subnetType === ec2.SubnetType.PRIVATE_WITH_NAT;
        if (isEgress && eigw) {
          new ec2.CfnRoute(this, `${logicalId}EgressRouteV6`, {
            routeTableId: rtb.ref,
            destinationIpv6CidrBlock: '::/0',
            egressOnlyInternetGatewayId: eigw.attrId,
          });
        }
        if (isEgress) egressRtbByAz.push({ azIdx, rtb });

        const subnet = ec2.Subnet.fromSubnetAttributes(this, `${logicalId}Ref`, {
          subnetId: cfnSubnet.ref,
          availabilityZone: az,
          routeTableId: rtb.ref,
          ...(hasV4Subnets ? { ipv4CidrBlock: subnetCidr } : {}),
        });

        bucket.push(subnet);
        this.allSubnets.push(subnet);
        subnetIndex++;
      }
    }

    // --- NAT gateways (IPv4 egress for private-with-egress subnets) -----------
    // One per AZ, in that AZ's public subnet; each AZ's egress subnets route to their own AZ's
    // NAT, so no AZ depends on another for egress. None in ipv6 mode: no IPv4 to translate.
    if (props.natGatewayPerAz && hasV4Subnets) {
      if (!publicSubnetByAz.some((x) => x)) {
        throw new Error('ParameterizedVpc: NAT gateways need a PUBLIC subnet tier.');
      }
      const natFor = new Map<number, string>();
      for (let azIdx = 0; azIdx < azCount; azIdx++) {
        const subnetId = publicSubnetByAz[azIdx];
        if (!subnetId) throw new Error(`ParameterizedVpc: no PUBLIC subnet in AZ ${azIdx + 1} for its NAT gateway.`);
        const suffix = `${azIdx + 1}`;
        const eip = new ec2.CfnEIP(this, `NatEip${suffix}`, { domain: 'vpc' });
        const nat = new ec2.CfnNatGateway(this, `NatGateway${suffix}`, {
          subnetId,
          allocationId: eip.attrAllocationId,
        });
        // Public subnets only exist with an IGW, so the attachment does too.
        eip.addDependency(igwAttachment!);
        nat.addDependency(igwAttachment!);
        natFor.set(azIdx, nat.ref);
        this.natGatewayIds.push(nat.ref);
      }
      egressRtbByAz.forEach(({ azIdx, rtb }, i) => {
        new ec2.CfnRoute(this, `PrivateDefaultRoute${i}`, {
          routeTableId: rtb.ref,
          destinationCidrBlock: '0.0.0.0/0',
          natGatewayId: natFor.get(azIdx)!,
        });
      });
    }

    // --- Expose an IVpc so downstream L2 constructs work --------------------
    this.vpc = ec2.Vpc.fromVpcAttributes(this, 'Vpc', {
      vpcId: this.vpcId,
      vpcCidrBlock: this.vpcCidrBlock,
      availabilityZones: azs,
      publicSubnetIds: this.publicSubnets.map((s) => s.subnetId),
      publicSubnetRouteTableIds: this.publicSubnets.map((s) => s.routeTable.routeTableId),
      privateSubnetIds: this.privateSubnets.map((s) => s.subnetId),
      privateSubnetRouteTableIds: this.privateSubnets.map((s) => s.routeTable.routeTableId),
      isolatedSubnetIds: this.isolatedSubnets.map((s) => s.subnetId),
      isolatedSubnetRouteTableIds: this.isolatedSubnets.map((s) => s.routeTable.routeTableId),
    });
  }

  // --- helpers --------------------------------------------------------------

  private bucketFor(type: ec2.SubnetType): ec2.ISubnet[] {
    switch (type) {
      case ec2.SubnetType.PUBLIC:
        return this.publicSubnets;
      case ec2.SubnetType.PRIVATE_WITH_EGRESS:
      case ec2.SubnetType.PRIVATE_WITH_NAT:
        return this.privateSubnets;
      case ec2.SubnetType.PRIVATE_ISOLATED:
        return this.isolatedSubnets;
      default:
        throw new Error(`Unsupported subnet type: ${type}`);
    }
  }
}
