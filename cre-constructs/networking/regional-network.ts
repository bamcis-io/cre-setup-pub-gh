import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';
import { IpMode, ParameterizedVpc, SubnetSpec } from './parameterized-vpc.js';

/**
 * A regional network: a deploy-time-CIDR VPC plus its endpoints.
 *
 *   - A plain `Construct`, so it composes inside any stack (one stack per region in a
 *     multi-region app).
 *   - `subnets: SubnetSpec[]` sets the topology (default: one PRIVATE_ISOLATED tier). The
 *     `privateNetwork()` / `egressOnly()` static factories cover the two standard shapes.
 *   - `ipMode` makes the whole network IPv4, dual-stack or IPv6-only.
 *   - S3 and DynamoDB gateway endpoints are always added (they cost nothing); interface
 *     endpoints default to [] (each costs money).
 *   - `scopedIngress: ScopedIngress[]` restricts an interface endpoint's security group to a
 *     CIDR, using a raw CfnSecurityGroupIngress so token CIDRs work.
 *   - peeringRouteTableIds is collected from every non-public subnet tier (the tiers that
 *     originate cross-region traffic).
 */

export interface ScopedIngress {
  /** The interface endpoint service whose SG should be scoped. */
  readonly service: ec2.InterfaceVpcEndpointAwsService;
  /** IPv4 CIDR allowed to reach it on 443. May be a CfnParameter token. */
  readonly allowCidr?: string;
  /** IPv6 CIDR allowed to reach it on 443 (needed in `ipv6` mode). May be a token. */
  readonly allowCidrIpv6?: string;
}

export interface RegionalNetworkProps {
  /** VPC CIDR for THIS region. Usually a CfnParameter token. */
  readonly vpcCidr: string;
  /** Subnet tiers. Default: a single PRIVATE_ISOLATED tier. */
  readonly subnets?: SubnetSpec[];
  /** AZ count. Default 3. */
  readonly azCount?: number;
  /** One NAT per AZ (requires a PUBLIC tier; ignored in `ipv6` mode). Default false. */
  readonly natGatewayPerAz?: boolean;
  /**
   * IPv4, dual-stack or IPv6-only. Default `ipv4`. In `ipv6` mode interface endpoints are created
   * IPv6-only (AAAA records) and their security group admits the VPC's IPv6 /56, so only services
   * whose endpoints support IPv6 can be used; dual-stack keeps endpoints on IPv4, which every
   * service supports and dual-stack clients can reach.
   */
  readonly ipMode?: IpMode;
  /** Force an IGW even with no public subnets (CloudFront VPC origins). Default false. */
  readonly includeInternetGateway?: boolean;
  /** Interface endpoints. Default [] (each costs money — opt in). */
  readonly interfaceEndpoints?: ec2.InterfaceVpcEndpointAwsService[];
  /** Scope a named interface endpoint's SG to a CIDR (token-safe raw ingress). */
  readonly scopedIngress?: ScopedIngress[];
}

/** Default subnet topology: a single private-isolated tier (cheap, no NAT/IGW). */
const DEFAULT_SUBNETS: SubnetSpec[] = [
  { name: 'Isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
];

/** Gateway endpoints: both free, so every network gets them. */
const GATEWAY_ENDPOINTS = [
  ec2.GatewayVpcEndpointAwsService.S3,
  ec2.GatewayVpcEndpointAwsService.DYNAMODB,
];

/**
 * A region's network: a deploy-time-CIDR VPC + gateway/interface endpoints, with
 * the set of route tables that should receive cross-region peering routes
 * collected for `PeeringMesh` to consume.
 *
 * Defaults are tuned to be cheap to leave running (isolated subnets, free S3/DDB
 * gateway endpoints, no NAT, no interface endpoints).
 */
export class RegionalNetwork extends Construct {
  public readonly vpc: ec2.IVpc;
  /** Route tables that should receive cross-region peering routes (consumed by `PeeringMesh`). */
  public readonly peeringRouteTableIds: string[];
  /** Interface endpoints, keyed by service shortName, for downstream consumers. */
  public readonly interfaceEndpoints: Record<string, ec2.IInterfaceVpcEndpoint> = {};
  public readonly ipMode: IpMode;
  /** The VPC's Amazon-provided IPv6 /56 (`dualstack` and `ipv6` modes). */
  public readonly vpcIpv6CidrBlock?: string;
  /** The underlying VPC builder (subnet lists, gateway ids). */
  public readonly vpcBuilder: ParameterizedVpc;

  constructor(scope: Construct, id: string, props: RegionalNetworkProps) {
    super(scope, id);

    const subnets = props.subnets ?? DEFAULT_SUBNETS;

    const vpcHelper = new ParameterizedVpc(this, 'Vpc', {
      vpcCidr: props.vpcCidr,
      subnets,
      azCount: props.azCount,
      natGatewayPerAz: props.natGatewayPerAz,
      includeInternetGateway: props.includeInternetGateway,
      ipMode: props.ipMode,
    });

    this.vpc = vpcHelper.vpc;
    this.vpcBuilder = vpcHelper;
    this.ipMode = vpcHelper.ipMode;
    this.vpcIpv6CidrBlock = vpcHelper.vpcIpv6CidrBlock;
    const v6Only = this.ipMode === 'ipv6';

    // Peering routes go on the workload-bearing (non-public) tiers — the tiers
    // that originate cross-region traffic. Public subnets route to the IGW, not
    // to peers. This covers private-with-egress, isolated and any custom topology.
    this.peeringRouteTableIds = [
      ...vpcHelper.privateSubnets,
      ...vpcHelper.isolatedSubnets,
    ].map((s) => s.routeTable.routeTableId);

    // Interface endpoints attach to the workload-bearing tier. Prefer
    // private-with-egress, then isolated, then public (best-effort).
    const endpointSubnetType = this.endpointSubnetType(subnets);

    // --- Gateway endpoints (free, always) ------------------------------------
    // They add prefix-list routes to every subnet's route table, so S3/DynamoDB traffic stays
    // off the NAT (which bills per GB) and works from isolated subnets.
    GATEWAY_ENDPOINTS.forEach((service) => {
      // shortName is e.g. 's3', 'dynamodb'; capitalize for a stable logical id.
      const idPart = service.name.split('.').pop() ?? service.name;
      this.vpc.addGatewayEndpoint(`${idPart}GatewayVpce`, { service });
    });

    // --- Scoped ingress SGs (token-safe) ------------------------------------
    // Built ahead of the interface-endpoint loop so a scoped service gets its
    // dedicated SG instead of the default open one.
    const scopedSgByService = new Map<ec2.InterfaceVpcEndpointAwsService, ec2.ISecurityGroup>();
    (props.scopedIngress ?? []).forEach((scoped) => {
      if (!scoped.allowCidr && !scoped.allowCidrIpv6) {
        throw new Error(`RegionalNetwork: scopedIngress for ${scoped.service.shortName} needs allowCidr or allowCidrIpv6.`);
      }
      const sg = new ec2.SecurityGroup(this, `${scoped.service.shortName}ScopedSg`, {
        vpc: this.vpc,
        description: `Allow HTTPS from a scoped CIDR to ${scoped.service.shortName}`,
        allowAllOutbound: false,
      });
      // CfnSecurityGroupIngress instead of ec2.Peer.ipv4(): the L2 helper
      // validates the CIDR at synth time and rejects CfnParameter tokens, but
      // we're passing a token here. CFN does the validation at deploy time
      // against the resolved value.
      if (scoped.allowCidr) {
        new ec2.CfnSecurityGroupIngress(this, `${scoped.service.shortName}ScopedSgIngress`, {
          groupId: sg.securityGroupId,
          ipProtocol: 'tcp',
          fromPort: 443,
          toPort: 443,
          cidrIp: scoped.allowCidr,
          description: 'HTTPS from scoped CIDR',
        });
      }
      if (scoped.allowCidrIpv6) {
        new ec2.CfnSecurityGroupIngress(this, `${scoped.service.shortName}ScopedSgIngressV6`, {
          groupId: sg.securityGroupId,
          ipProtocol: 'tcp',
          fromPort: 443,
          toPort: 443,
          cidrIpv6: scoped.allowCidrIpv6,
          description: 'HTTPS from scoped IPv6 CIDR',
        });
      }
      scopedSgByService.set(scoped.service, sg);
    });

    // --- Interface endpoints (empty by default — opt in) --------------------
    const interfaceEndpoints = props.interfaceEndpoints ?? [];
    interfaceEndpoints.forEach((service) => {
      const scopedSg = scopedSgByService.get(service);
      const endpoint = this.vpc.addInterfaceEndpoint(`${service.shortName}Vpce`, {
        service,
        subnets: { subnetType: endpointSubnetType },
        // Private DNS stays enabled so the service's regional name resolves to the endpoint.
        privateDnsEnabled: true,
        ...(scopedSg ? { securityGroups: [scopedSg] } : { open: true }),
        // IPv6-only subnets can only hold IPv6-only endpoints.
        ...(v6Only
          ? { ipAddressType: ec2.VpcEndpointIpAddressType.IPV6, dnsRecordIpType: ec2.VpcEndpointDnsRecordIpType.IPV6 }
          : {}),
      });
      // `open: true` only admits the VPC's IPv4 CIDR. In ipv6 mode nothing has an IPv4 address,
      // so admit the IPv6 /56 (raw ingress: the CIDR is a token).
      if (v6Only && !scopedSg) {
        new ec2.CfnSecurityGroupIngress(this, `${service.shortName}VpceIngressV6`, {
          groupId: endpoint.connections.securityGroups[0].securityGroupId,
          ipProtocol: 'tcp',
          fromPort: 443,
          toPort: 443,
          cidrIpv6: this.vpcIpv6CidrBlock!,
          description: 'HTTPS from the VPC IPv6 CIDR',
        });
      }
      this.interfaceEndpoints[service.shortName] = endpoint;
    });
  }

  /**
   * The private network: isolated subnets only, with an internet gateway attached but never routed
   * to (CloudFront VPC origins require the attachment). Nothing in it reaches the internet; AWS
   * services are reached through the gateway endpoints and whatever `interfaceEndpoints` you add.
   * This is where Locust tasks and internal ALBs live.
   */
  static privateNetwork(
    scope: Construct,
    id: string,
    props: Omit<RegionalNetworkProps, 'subnets' | 'natGatewayPerAz' | 'includeInternetGateway'>,
  ): RegionalNetwork {
    return new RegionalNetwork(scope, id, {
      ...props,
      includeInternetGateway: true,
      subnets: [{ name: 'Isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED }],
    });
  }

  /**
   * The egress-only network: private subnets that reach the internet outbound only.
   *   - `ipv4` / `dualstack`: a public tier holding one NAT gateway per AZ, and a private tier
   *     whose IPv4 default route goes to its own AZ's NAT. Dual-stack adds an egress-only internet
   *     gateway for IPv6 (`::/0`).
   *   - `ipv6`: a single IPv6-only private tier routing `::/0` to the egress-only internet
   *     gateway. No NAT and no public tier (there is no IPv4 to translate, and no NAT64).
   * The internet gateway is always attached, so CloudFront VPC origins work here too.
   */
  static egressOnly(
    scope: Construct,
    id: string,
    props: Omit<RegionalNetworkProps, 'subnets' | 'natGatewayPerAz' | 'includeInternetGateway'>,
  ): RegionalNetwork {
    const v6Only = props.ipMode === 'ipv6';
    return new RegionalNetwork(scope, id, {
      ...props,
      includeInternetGateway: true,
      natGatewayPerAz: !v6Only,
      subnets: [
        ...(v6Only ? [] : [{ name: 'Public', subnetType: ec2.SubnetType.PUBLIC }]),
        { name: 'Private', subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      ],
    });
  }

  /** Pick the workload-bearing tier interface endpoints should attach to. */
  private endpointSubnetType(subnets: SubnetSpec[]): ec2.SubnetType {
    const types = new Set(subnets.map((s) => s.subnetType));
    if (types.has(ec2.SubnetType.PRIVATE_WITH_EGRESS)) {
      return ec2.SubnetType.PRIVATE_WITH_EGRESS;
    }
    if (types.has(ec2.SubnetType.PRIVATE_WITH_NAT)) {
      return ec2.SubnetType.PRIVATE_WITH_NAT;
    }
    if (types.has(ec2.SubnetType.PRIVATE_ISOLATED)) {
      return ec2.SubnetType.PRIVATE_ISOLATED;
    }
    return ec2.SubnetType.PUBLIC;
  }
}
