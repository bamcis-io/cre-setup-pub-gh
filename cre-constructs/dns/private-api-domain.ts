import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import { Construct } from 'constructs';
import { CreCfnParameter } from '../core/cre-cfn-parameter.js';
import { CreStack, CreStackProps, requireDeployRegion } from '../core/cre-stack.js';
import { DNS_CONTROL_PLANE_REGIONS } from '../core/partitions.js';

/**
 * Custom domain names for a PRIVATE REST API, reached through its execute-api VPC endpoint, from
 * anywhere that can route to that endpoint (the same VPC, or a peered one), with TLS on your own
 * domain and Route 53 routing (weighted, health-checked) between regions.
 *
 * Two constructs, because the pieces live in different stacks (and often different accounts):
 *   - {@link PrivateApiCustomDomain}, in each region's API stack, next to the API and its VPC
 *     endpoint: the custom domain names, base path mappings and endpoint access associations, plus
 *     outputs naming the endpoint's DNS name.
 *   - {@link PrivateApiDnsRecords} (or {@link CreDnsStack.addPrivateApi}), in the `-dns` stack:
 *     the alias records, taking each region's endpoint from its PrivateApiCustomDomain through
 *     CreCfnParameters (the pipeline passes them; no exports).
 *
 * Hostnames for `name: 'api'`: `api-<region>.<domain>` per region (a dash, not a dot: the wildcard
 * certificate `*.<domain>` covers one label only) and `api.<domain>`, the shared name every region
 * also serves, weighted across regions.
 */

const NAME = /^[a-z][a-z0-9]*$/;

function apiKey(name: string): string {
  if (!NAME.test(name)) {
    throw new Error(`Private API name '${name}' must be lowercase letters and digits, starting with a letter (e.g. 'api').`);
  }
  return name[0].toUpperCase() + name.slice(1);
}

const REGION = /^[a-z]{2,4}(-[a-z]+)+-\d+$/; // us-east-1, us-gov-west-1, cn-north-1, eusc-de-east-1

/** `us-east-1` → `UsEast1`; region names in the records stack's parameter names are data, not the stack's own region. */
function regionKey(region: string, who: string): string {
  if (cdk.Token.isUnresolved(region) || !REGION.test(region)) {
    throw new Error(`${who}: region must be a literal region name like 'us-east-1' (got ${cdk.Token.isUnresolved(region) ? 'a token' : `'${region}'`}). ` +
      'These are the regions the API stacks are deployed in (their region prop).');
  }
  return region.split('-').map((p) => p[0].toUpperCase() + p.slice(1)).join('');
}


/**
 * The outputs {@link PrivateApiCustomDomain} adds to its stack: `PrivateApi<Name>VpceDnsName` and
 * `PrivateApi<Name>VpceHostedZoneId`. The same names in every region: each `-dns` parameter names
 * the stack it comes from.
 */
export function privateApiOutputNames(name: string): { vpceDnsName: string; vpceHostedZoneId: string } {
  const base = `PrivateApi${apiKey(name)}`;
  return { vpceDnsName: `${base}VpceDnsName`, vpceHostedZoneId: `${base}VpceHostedZoneId` };
}

/**
 * The `-dns` stack parameters {@link PrivateApiDnsRecords} declares for one of the API's regions:
 * `PrivateApi<Name><Region>VpceDnsName` / `...VpceHostedZoneId`, e.g.
 * `PrivateApiApiUsWest2VpceDnsName`. Fed from that region's {@link privateApiOutputNames} outputs.
 */
export function privateApiRecordParameterNames(name: string, region: string): { vpceDnsName: string; vpceHostedZoneId: string } {
  const base = `PrivateApi${apiKey(name)}${regionKey(region, 'privateApiRecordParameterNames')}`;
  return { vpceDnsName: `${base}VpceDnsName`, vpceHostedZoneId: `${base}VpceHostedZoneId` };
}

/**
 * The custom domain names' resource policy. Without one, API Gateway requires SigV4 on every call
 * (403 for a plain HTTPS client). Allow-all is safe: only the VPC endpoints named by the access
 * associations can reach the names.
 */
const ALLOW_ALL_POLICY = {
  Version: '2012-10-17',
  Statement: [{ Effect: 'Allow', Principal: '*', Action: 'execute-api:Invoke', Resource: ['execute-api:/*'] }],
};

export interface PrivateApiCustomDomainProps {
  /** Hostname label and key, e.g. `api` → `api-<region>.<domain>` and `api.<domain>`. */
  readonly name: string;
  /** The project's domain (`DOMAIN`; usually a CfnParameter token). */
  readonly domain: string;
  /**
   * A certificate covering `*.<domain>` in this region: the region's `ValidatedCertificate`
   * `certificateArn` (one per project per region, so it's passed in, not created here).
   */
  readonly certificateArn: string;
  /** The PRIVATE REST API. Its resource policy must not carry a `aws:SourceVpce` condition. */
  readonly restApi: apigateway.IRestApi;
  /** Stage to map. Default the RestApi's deployment stage, else `prod`. */
  readonly stage?: string;
  /** The execute-api interface endpoint clients connect through (in this region and VPC). */
  readonly vpcEndpoint: ec2.IInterfaceVpcEndpoint;
  /** Base path on the domain names. Default none (the API at `/`). */
  readonly basePath?: string;
}

/**
 * The regional half: put it in the API's stack. Creates, for `api-<region>.<domain>` and
 * `api.<domain>`: a private `AWS::ApiGateway::DomainNameV2` (V1 `DomainName` rejects PRIVATE) with
 * an allow-all policy, a `BasePathMappingV2` to the stage, and a `DomainNameAccessAssociation` for
 * the VPC endpoint (without it every call is 403). Outputs the endpoint's REGIONAL DNS name and
 * hosted zone id (a zonal name is rejected as an alias target) as {@link privateApiOutputNames}.
 * The region in the hostname is `AWS::Region`, so the same template serves every region.
 */
export class PrivateApiCustomDomain extends Construct {
  /** `api-<region>.<domain>`. */
  public readonly regionalDomainName: string;
  /** `api.<domain>`. */
  public readonly sharedDomainName: string;
  public readonly regionalDomain: apigateway.CfnDomainNameV2;
  public readonly sharedDomain: apigateway.CfnDomainNameV2;
  /** The `name` it was created with. */
  public readonly name: string;
  /** The stack it is in (a top-level CreStack): the records stack takes its outputs from there. */
  public readonly stack: CreStack;
  /** The endpoint's regional DNS name and hosted zone id, as output. */
  public readonly vpceDnsName: string;
  public readonly vpceHostedZoneId: string;

  constructor(scope: Construct, id: string, props: PrivateApiCustomDomainProps) {
    super(scope, id);
    this.name = props.name;
    const own = cdk.Stack.of(this);
    if (!(own instanceof CreStack)) throw new Error('PrivateApiCustomDomain: must be in a top-level CreStack (its outputs feed the -dns stack).');
    this.stack = own;
    const names = privateApiOutputNames(props.name);
    const stage = props.stage ?? (props.restApi instanceof apigateway.RestApi ? props.restApi.deploymentStage.stageName : 'prod');
    this.regionalDomainName = cdk.Fn.join('', [`${props.name}-`, Aws.REGION, '.', props.domain]);
    this.sharedDomainName = cdk.Fn.join('', [`${props.name}.`, props.domain]);

    const domainFor = (key: string, domainName: string) => {
      const domain = new apigateway.CfnDomainNameV2(this, `${key}Domain`, {
        domainName,
        endpointConfiguration: { types: ['PRIVATE'] },
        certificateArn: props.certificateArn,
        securityPolicy: 'TLS_1_2',
        policy: ALLOW_ALL_POLICY,
      });
      new apigateway.CfnBasePathMappingV2(this, `${key}Mapping`, {
        domainNameArn: domain.attrDomainNameArn,
        restApiId: props.restApi.restApiId,
        stage,
        basePath: props.basePath,
      });
      new apigateway.CfnDomainNameAccessAssociation(this, `${key}Access`, {
        domainNameArn: domain.attrDomainNameArn,
        accessAssociationSource: props.vpcEndpoint.vpcEndpointId,
        accessAssociationSourceType: 'VPCE',
      });
      return domain;
    };
    this.regionalDomain = domainFor('Regional', this.regionalDomainName);
    this.sharedDomain = domainFor('Shared', this.sharedDomainName);

    // DnsEntries are "<hosted zone id>:<dns name>"; the first is the regional (AZ-less) name.
    const entry = cdk.Fn.select(0, this.dnsEntries(props.vpcEndpoint));
    this.vpceDnsName = cdk.Fn.select(1, cdk.Fn.split(':', entry));
    this.vpceHostedZoneId = cdk.Fn.select(0, cdk.Fn.split(':', entry));
    new cdk.CfnOutput(own, names.vpceDnsName, { value: this.vpceDnsName });
    new cdk.CfnOutput(own, names.vpceHostedZoneId, { value: this.vpceHostedZoneId });
  }

  private dnsEntries(endpoint: ec2.IInterfaceVpcEndpoint): string[] {
    if (endpoint instanceof ec2.InterfaceVpcEndpoint) return endpoint.vpcEndpointDnsEntries;
    throw new Error('PrivateApiCustomDomain: vpcEndpoint must be an ec2.InterfaceVpcEndpoint created in this app (its DnsEntries are needed).');
  }
}

export interface PrivateApiDnsRecordsProps {
  /**
   * The API's {@link PrivateApiCustomDomain} in each region, in order (the first is the default
   * weight-100 target); all with the same `name`, each in a stack deployed to a different region.
   * Each becomes `<name>-<region>.<domain>` and a weighted record on the shared name, aliased to
   * that region's endpoint, which arrives as the `PrivateApi<Name><Region>Vpce*` parameters.
   */
  readonly domains: PrivateApiCustomDomain[];
  /** The project's domain (the `-dns` stack's `Domain` parameter). */
  readonly domain: string;
  /** The public hosted zone (the `-dns` stack's `HostedZoneId` parameter). */
  readonly hostedZoneId: string;
  /**
   * Weight of each region on the shared name, in `domains` order. Default the first 100, the rest 0
   * (active/standby: Route 53 answers with a weight-0 record only when every weighted record
   * above 0 is unhealthy, so pair it with `healthCheckIds`).
   */
  readonly weights?: number[];
  /** Route 53 health check id per region on the shared name, in `domains` order (e.g. ARC Region switch's). */
  readonly healthCheckIds?: string[];
  /**
   * Record types. Default `['A']`. Add `'AAAA'` when the endpoint has IPv6 addresses (a
   * `dualstack` or `ipv6` network).
   */
  readonly recordTypes?: ('A' | 'AAAA')[];
}

/**
 * The records half: put it in the `-dns` stack (`CreDnsStack.addPrivateApi` does it for you).
 * Declares the `PrivateApi<Name><Region>Vpce*` parameters (CreCfnParameters taking the regions'
 * outputs) and creates `api-<region>.<domain>` (alias to that region's endpoint) per region, and
 * `api.<domain>` weighted across the regions.
 */
export class PrivateApiDnsRecords extends Construct {
  constructor(scope: Construct, id: string, props: PrivateApiDnsRecordsProps) {
    super(scope, id);
    const domains = props.domains;
    if (!domains || domains.length === 0) throw new Error('PrivateApiDnsRecords: give the API\'s PrivateApiCustomDomain in each region.');
    const name = domains[0].name;
    if (domains.some((d) => d.name !== name)) throw new Error('PrivateApiDnsRecords: every domain must have the same name.');
    const regions = domains.map((d) => d.stack.deployRegion);
    if (new Set(regions).size !== regions.length) throw new Error('PrivateApiDnsRecords: two domains are deployed in the same region.');
    regions.forEach((r) => regionKey(r, 'PrivateApiDnsRecords'));
    const weights = props.weights ?? regions.map((_, i) => (i === 0 ? 100 : 0));
    if (weights.length !== regions.length) throw new Error('PrivateApiDnsRecords: give one weight per region.');
    if (props.healthCheckIds && props.healthCheckIds.length !== regions.length) {
      throw new Error('PrivateApiDnsRecords: give one health check id per region.');
    }
    const types = props.recordTypes ?? ['A'];
    const key = apiKey(name);
    const outputs = privateApiOutputNames(name);

    regions.forEach((region, i) => {
      const d = domains[i];
      const names = privateApiRecordParameterNames(name, region);
      const target = {
        dnsName: new CreCfnParameter(this, names.vpceDnsName, {
          from: { stack: d.stack, value: d.vpceDnsName, outputName: outputs.vpceDnsName },
          description: `Regional DNS name of '${name}''s execute-api endpoint in ${region}.`,
        }).valueAsString,
        hostedZoneId: new CreCfnParameter(this, names.vpceHostedZoneId, {
          from: { stack: d.stack, value: d.vpceHostedZoneId, outputName: outputs.vpceHostedZoneId },
          description: 'Hosted zone of that endpoint DNS name.',
        }).valueAsString,
        evaluateTargetHealth: true,
      };
      const rk = regionKey(region, 'PrivateApiDnsRecords');
      for (const type of types) {
        new route53.CfnRecordSet(this, `${key}${rk}${type}`, {
          hostedZoneId: props.hostedZoneId,
          name: cdk.Fn.join('', [`${name}-`, region, '.', props.domain]),
          type,
          aliasTarget: target,
        });
        new route53.CfnRecordSet(this, `${key}Shared${rk}${type}`, {
          hostedZoneId: props.hostedZoneId,
          name: cdk.Fn.join('', [`${name}.`, props.domain]),
          type,
          setIdentifier: region,
          weight: weights[i],
          healthCheckId: props.healthCheckIds?.[i],
          aliasTarget: target,
        });
      }
    });
  }
}

/**
 * The `$PROJECT_NAME-dns` stack: the project's public records. Takes `Domain` and `HostedZoneId`
 * from the DOMAIN and HOSTED_ZONE_ID deploy variables. Always in its partition's Route 53 control-plane region
 * ({@link DNS_CONTROL_PLANE_REGIONS}: us-east-1 in `aws`): the synth refuses another `region`, and a
 * CloudFormation Rule fails a deploy anywhere else. For a zone in another account, deploy it with
 * `assumeRole: '$DNS_ROLE_ARN?'` and `executionRole: '$DNS_CFN_EXECUTION_ROLE_ARN?'`. What the
 * records point at arrives through CreCfnParameters from the stacks that own the targets.
 *
 * ```ts
 * const dns = new CreDnsStack(app, app.stackId('dns'), { region: 'us-east-1',
 *   assumeRole: '$DNS_ROLE_ARN?', executionRole: '$DNS_CFN_EXECUTION_ROLE_ARN?' });
 * ```
 */
/** What a CreDnsStack in the zone's account may create: setup's CreDnsCfnExecution allows records only. */
export const DNS_ACCOUNT_RESOURCE_TYPES = ['AWS::Route53::RecordSet', 'AWS::Route53::RecordSetGroup'];

export class CreDnsStack extends CreStack {
  public readonly domain: string;
  public readonly hostedZoneId: string;

  constructor(scope: Construct, id: string, props: CreStackProps) {
    super(scope, id, props);
    this.creKind = 'dns';
    if (!Object.values(DNS_CONTROL_PLANE_REGIONS).includes(props.region)) {
      throw new Error(`CreDnsStack ${id}: region must be its partition's Route 53 control-plane region (${Object.values(DNS_CONTROL_PLANE_REGIONS).join(', ')}), not ${props.region}.`);
    }
    requireDeployRegion(this, 'DnsControlPlaneRegion', Object.values(DNS_CONTROL_PLANE_REGIONS),
      "the project's public DNS records live in its partition's Route 53 control-plane region");
    this.domain = new CreCfnParameter(this, 'Domain', { fromVariable: 'DOMAIN', description: 'The project domain (DOMAIN).' }).valueAsString;
    this.hostedZoneId = new CreCfnParameter(this, 'HostedZoneId', {
      fromVariable: 'HOSTED_ZONE_ID', description: 'Its public hosted zone (HOSTED_ZONE_ID).',
    }).valueAsString;
    // In the zone's account (assumeRole), the deploy runs as CreDnsDeploy and CloudFormation as
    // CreDnsCfnExecution, which setup (dns.py) lets do exactly this: the `<project>-dns` stack, records
    // in the one zone. Anything else would synth and fail at deploy, after the merge (review #14).
    if (props.assumeRole) {
      this.node.addValidation({
        validate: () => {
          const problems: string[] = [];
          if (!id.endsWith('-dns')) {
            problems.push(`CreDnsStack ${id}: deployed into the zone's account (assumeRole), it must be the project's DNS stack, app.stackId('dns'): ` +
              "setup's roles there allow only that stack.");
          }
          for (const c of this.node.findAll()) {
            if (c instanceof cdk.CfnResource && !DNS_ACCOUNT_RESOURCE_TYPES.includes(c.cfnResourceType)) {
              problems.push(`CreDnsStack ${id}: ${c.node.path} is a ${c.cfnResourceType}; in the zone's account this stack may hold only ` +
                `${DNS_ACCOUNT_RESOURCE_TYPES.join(', ')} (CreDnsCfnExecution's permissions). Put it in a stack in the deploy account.`);
            }
          }
          return problems;
        },
      });
    }
  }

  /** The records for a private API's custom domain names (see {@link PrivateApiDnsRecords}). */
  public addPrivateApi(props: Omit<PrivateApiDnsRecordsProps, 'domain' | 'hostedZoneId'>): PrivateApiDnsRecords {
    const name = props.domains[0]?.name ?? 'api';
    return new PrivateApiDnsRecords(this, `PrivateApi${apiKey(name)}`, { ...props, domain: this.domain, hostedZoneId: this.hostedZoneId });
  }
}
