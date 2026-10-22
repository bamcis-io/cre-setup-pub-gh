import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Construct } from 'constructs';
import { CreCfnParameter } from '../../core/cre-cfn-parameter.js';
import { CreStack, CreStackProps } from '../../core/cre-stack.js';
import { DistributionSigner } from './distribution-signer.js';
import { CreEdgeOrigin, edgeOriginOutputNames, edgeRouteKey } from './edge-origin.js';

export interface CreEdgeRoute {
  /** Route name, e.g. `locust`, `app`. */
  readonly name: string;
  /**
   * CloudFront path pattern, e.g. `/locust*` or `/api/*`; `/*` makes this the default route.
   * CloudFront does not rewrite paths: the origin must serve under this prefix (Locust does, via
   * `--web-base-path`, which `LoadGenerator` sets).
   */
  readonly pathPattern: string;
  /**
   * How CloudFront reaches the ALB: `ipv4`, or `dualstack` (needs a dual-stack ALB in dual-stack
   * subnets). VPC origins have no IPv6-only option, so an IPv6-only network can't be a VPC origin.
   * @default ipv4 (CloudFront's default; the property is left unset)
   */
  readonly ipAddressType?: 'ipv4' | 'dualstack';
}

/** The Locust UI route `LoadGenerator` (exposeWebUi) publishes. */
export const LOCUST_EDGE_ROUTE: CreEdgeRoute = { name: 'locust', pathPattern: '/locust*' };

/** A route and the origin that serves it. */
export interface CreEdgeStackRoute extends CreEdgeRoute {
  /** The ALB behind the route: a {@link CreEdgeOrigin} in its own stack (e.g. `loadGen.edgeOrigin`). */
  readonly origin: CreEdgeOrigin;
}

export interface CreEdgeStackProps extends CreStackProps {
  /** The routes, each with its origin, e.g. `[{ ...LOCUST_EDGE_ROUTE, origin: loadGen.edgeOrigin! }]`. */
  readonly routes: CreEdgeStackRoute[];
  /** Distribution comment. @default `<stack name>: CloudFront Signer edge` */
  readonly comment?: string;
}

/**
 * The project's edge: ONE CloudFront distribution behind CloudFront Signer (Midway + bindle),
 * with path-based routes to internal ALBs in other stacks through VPC origins. Deploy it in
 * us-east-1 (CloudFront Signer reads its signing secret there; commercial partition only),
 * whatever region the origins are in. Each route's `Edge<Name>AlbArn` / `Edge<Name>AlbDnsName`
 * parameters take its {@link CreEdgeOrigin}'s outputs, and `BindleId` the BINDLE_ID deploy
 * variable, so the pipeline deploys it after the origins' stacks.
 *
 * The default behavior is the `/*` route if there is one, otherwise the first route. Outputs
 * `EdgeUrl` (the distribution), `Edge<Name>Url` per route, `EdgeDistributionId`, `EdgeDomainName`
 * (onboard it once at the CloudFront Signer portal after the first deploy) and `CfsSecretArn`.
 * `CreDashboardStack` links `EdgeUrl` and `EdgeLocustUrl` automatically.
 *
 * ```ts
 * new CreEdgeStack(app, app.stackId('edge'), { region: 'us-east-1',
 *   routes: [{ ...LOCUST_EDGE_ROUTE, origin: loadGen.edgeOrigin! }, { name: 'app', pathPattern: '/*', origin: appOrigin }] });
 * ```
 */
export class CreEdgeStack extends CreStack {
  public readonly distribution: cloudfront.Distribution;
  public readonly signer: DistributionSigner;
  /** Output values by output name (`EdgeUrl`, `Edge<Route>Url`, `EdgeDomainName`, ...), for CreCfnParameters elsewhere. */
  public readonly outputValues: Record<string, string> = {};

  constructor(scope: Construct, id: string, props: CreEdgeStackProps) {
    super(scope, id, props);
    this.creKind = 'edge';
    const routes = props.routes;
    if (routes.length === 0) throw new Error('CreEdgeStack: give at least one route.');
    const seen = new Set<string>();
    for (const r of routes) {
      edgeRouteKey(r.name);
      if (seen.has(r.name)) throw new Error(`CreEdgeStack: route '${r.name}' is listed twice.`);
      seen.add(r.name);
      if (!r.pathPattern.startsWith('/')) throw new Error(`CreEdgeStack: route '${r.name}' path pattern must start with '/'.`);
    }
    if (routes.filter((r) => r.pathPattern === '/*').length > 1) {
      throw new Error('CreEdgeStack: only one route can be the default (`/*`).');
    }

    const bindleId = new CreCfnParameter(this, 'BindleId', {
      fromVariable: 'BINDLE_ID',
      description: 'Bindle for CloudFront Signer (Midway) authorization (the BINDLE_ID deploy variable).',
    });

    const behaviorFor = (r: CreEdgeStackRoute): cloudfront.BehaviorOptions => {
      const names = edgeOriginOutputNames(r.name);
      if (r.origin.name !== r.name) throw new Error(`CreEdgeStack: route '${r.name}' has the origin of route '${r.origin.name}'.`);
      const originStack = r.origin.stack;
      if (!(originStack instanceof CreStack)) throw new Error(`CreEdgeStack: route '${r.name}''s origin must be in a top-level CreStack.`);
      const arn = new CreCfnParameter(this, names.albArn, {
        from: { stack: originStack, value: r.origin.loadBalancerArn },
        description: `Internal ALB for route '${r.name}' (output ${names.albArn} of ${originStack.stackName}).`,
      });
      const dns = new CreCfnParameter(this, names.albDnsName, { from: { stack: originStack, value: r.origin.loadBalancerDnsName } });
      // Imported by attributes from parameters (no cross-stack export). The security group id is a
      // placeholder: a VPC origin needs only the ARN; ingress is CreEdgeOrigin's job, in-region.
      const alb = elbv2.ApplicationLoadBalancer.fromApplicationLoadBalancerAttributes(this, `${edgeRouteKey(r.name)}Alb`, {
        loadBalancerArn: arn.valueAsString,
        loadBalancerDnsName: dns.valueAsString,
        securityGroupId: 'sg-00000000',
      });
      // The port the origin's listener (and its security group rule) uses.
      const originOptions = { protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY, httpPort: r.origin.port };
      let origin: cloudfront.IOrigin;
      if (r.ipAddressType) {
        // CDK has no prop for VpcOriginEndpointConfig.IpAddressType yet, so build the VpcOrigin
        // here and set it on the L1. Only then: the default path keeps the logical ids of
        // existing deployments.
        const vpcOrigin = new cloudfront.VpcOrigin(this, `${edgeRouteKey(r.name)}VpcOrigin`, {
          endpoint: cloudfront.VpcOriginEndpoint.applicationLoadBalancer(alb),
          ...originOptions,
        });
        (vpcOrigin.node.defaultChild as cloudfront.CfnVpcOrigin)
          .addPropertyOverride('VpcOriginEndpointConfig.IpAddressType', r.ipAddressType);
        origin = origins.VpcOrigin.withVpcOrigin(vpcOrigin);
      } else {
        origin = origins.VpcOrigin.withApplicationLoadBalancer(alb, originOptions);
      }
      return {
        origin,
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
      };
    };

    const defaultRoute = routes.find((r) => r.pathPattern === '/*') ?? routes[0];
    const behaviors = new Map(routes.map((r) => [r.name, behaviorFor(r)]));
    const additionalBehaviors: Record<string, cloudfront.BehaviorOptions> = {};
    for (const r of routes) {
      if (r.pathPattern !== '/*') additionalBehaviors[r.pathPattern] = behaviors.get(r.name)!;
    }
    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: props.comment ?? `${id}: CloudFront Signer edge`,
      defaultBehavior: behaviors.get(defaultRoute.name)!,
      additionalBehaviors,
    });
    this.signer = DistributionSigner.apply(this.distribution, { authorizerBindleId: bindleId.valueAsString });

    const url = `https://${this.distribution.distributionDomainName}`;
    const output = (name: string, value: string, description?: string) => {
      new cdk.CfnOutput(this, name, { value, description });
      this.outputValues[name] = value;
    };
    output('EdgeUrl', url, 'CloudFront Signer edge URL.');
    for (const r of routes) {
      const prefix = r.pathPattern === '/*' ? '/' : r.pathPattern.replace(/\*.*$/, '').replace(/\/?$/, '/');
      output(`Edge${edgeRouteKey(r.name)}Url`, `${url}${prefix}`, `Route '${r.name}'.`);
    }
    output('EdgeDistributionId', this.distribution.distributionId);
    output('EdgeDomainName', this.distribution.distributionDomainName,
      'Onboard this domain once at the CloudFront Signer portal after the first deploy.');
    output('CfsSecretArn', this.signer.secretArn);
  }
}
