import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

/** AWS-managed CloudFront origin-facing prefix lists (same name in every region, region-scoped id). */
const CLOUDFRONT_PREFIX_LISTS = [
  { id: 'V4', name: 'com.amazonaws.global.cloudfront.origin-facing' },
  { id: 'V6', name: 'com.amazonaws.global.ipv6.cloudfront.origin-facing' },
];

const NAME = /^[a-z][a-z0-9]{0,30}$/;

/** `locust` → `Locust`: the route name as it appears in output and parameter names. */
export function edgeRouteKey(name: string): string {
  if (!NAME.test(name)) {
    throw new Error(`Edge route name '${name}' must be lowercase letters and digits, starting with a letter (e.g. 'locust', 'app').`);
  }
  return name[0].toUpperCase() + name.slice(1);
}

/** The output / parameter names an origin publishes and {@link CreEdgeStack} reads. */
export function edgeOriginOutputNames(name: string): { albArn: string; albDnsName: string } {
  const k = edgeRouteKey(name);
  return { albArn: `Edge${k}AlbArn`, albDnsName: `Edge${k}AlbDnsName` };
}

export interface CreEdgeOriginProps {
  /**
   * Route name, matching a route in the project's {@link CreEdgeStack} (`locust`, `app`, ...).
   * Lowercase letters and digits. One origin per name per project.
   */
  readonly name: string;
  /** The internal ALB CloudFront reaches through a VPC origin. */
  readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  /** The ALB's VPC. It must have an internet gateway attached (`RegionalNetwork.privateNetwork()`, or `includeInternetGateway: true`). */
  readonly vpc: ec2.IVpc;
  /** Listener port CloudFront connects to (HTTP). @default 80 */
  readonly port?: number;
}

/**
 * The origin half of a CloudFront-Signer-protected route, in the ALB's own stack and region.
 *
 *   - Lets CloudFront's origin-facing ranges reach the internal ALB: one security group per
 *     AWS-managed prefix list (IPv4, IPv6), each looked up at deploy time because the list id is
 *     region-scoped. One group per list because each list counts against the per-group rule
 *     quota by its max size; both on one group would exceed the default quota.
 *   - Outputs `Edge<Name>AlbArn` and `Edge<Name>AlbDnsName` on the stack. Give the origin to
 *     {@link CreEdgeStack}'s route: its parameters of the same names take them (CreCfnParameter),
 *     and it builds the VPC origin.
 *
 * Prerequisite: the VPC has an internet gateway attached; CloudFront VPC origins need one even for
 * an internal ALB in isolated subnets. Without the ingress, CloudFront answers 502/504.
 *
 * `LoadGenerator` with `exposeWebUi` creates one of these (route `locust`) itself.
 */
export class CreEdgeOrigin extends Construct {
  public readonly name: string;
  /** The stack holding the ALB: the edge stack takes the ALB's ARN and DNS name from its outputs. */
  public readonly stack: cdk.Stack;
  public readonly loadBalancerArn: string;
  public readonly loadBalancerDnsName: string;
  /** The listener port CloudFront connects to (the edge stack's VPC origin uses it). */
  public readonly port: number;

  constructor(scope: Construct, id: string, props: CreEdgeOriginProps) {
    super(scope, id);
    this.name = props.name;
    const names = edgeOriginOutputNames(props.name);
    const stack = cdk.Stack.of(this);
    this.stack = stack;
    this.loadBalancerArn = props.loadBalancer.loadBalancerArn;
    this.loadBalancerDnsName = props.loadBalancer.loadBalancerDnsName;
    this.port = props.port ?? 80;
    const port = props.port ?? 80;

    if (stack.node.tryFindChild(names.albArn)) {
      throw new Error(`CreEdgeOrigin: a route named '${props.name}' already exists in ${stack.stackName}.`);
    }

    for (const pl of CLOUDFRONT_PREFIX_LISTS) {
      const lookup = prefixListLookup(stack, pl.id, pl.name);
      const sg = new ec2.SecurityGroup(this, `CloudFront${pl.id}`, {
        vpc: props.vpc,
        description: `HTTP ${port} from ${pl.name}`,
        allowAllOutbound: false,
      });
      new ec2.CfnSecurityGroupIngress(this, `CloudFront${pl.id}Ingress`, {
        groupId: sg.securityGroupId,
        ipProtocol: 'tcp',
        fromPort: port,
        toPort: port,
        sourcePrefixListId: lookup.getResponseField('PrefixLists.0.PrefixListId'),
        description: `HTTP from ${pl.name}`,
      });
      props.loadBalancer.addSecurityGroup(sg);
    }

    new cdk.CfnOutput(stack, names.albArn, {
      value: props.loadBalancer.loadBalancerArn,
      description: `Edge route '${props.name}': internal ALB the CloudFront VPC origin targets.`,
    });
    new cdk.CfnOutput(stack, names.albDnsName, {
      value: props.loadBalancer.loadBalancerDnsName,
      description: `Edge route '${props.name}': the ALB's DNS name.`,
    });
  }
}

/** One lookup per prefix list per stack, shared by every origin in it. */
function prefixListLookup(stack: cdk.Stack, id: string, name: string): cr.AwsCustomResource {
  const cid = `CreCloudFrontPrefixList${id}`;
  const existing = stack.node.tryFindChild(cid) as cr.AwsCustomResource | undefined;
  if (existing) return existing;
  return new cr.AwsCustomResource(stack, cid, {
    onUpdate: {
      service: 'EC2',
      action: 'describeManagedPrefixLists',
      parameters: { Filters: [{ Name: 'prefix-list-name', Values: [name] }] },
      physicalResourceId: cr.PhysicalResourceId.of(`${name}-${Aws.REGION}`),
    },
    policy: cr.AwsCustomResourcePolicy.fromStatements([
      new iam.PolicyStatement({ actions: ['ec2:DescribeManagedPrefixLists'], resources: ['*'] }),
    ]),
    installLatestAwsSdk: false,
  });
}
