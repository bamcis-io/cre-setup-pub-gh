import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { Construct } from 'constructs';

/**
 * Props for {@link AllowedCidrSecurityGroup}.
 *
 * The construct is the always-on auth baseline. It
 * synthesizes green with only a `vpc` — the integration contract every demo's `app.ts`
 * stub relies on (callers pass ONLY `{ vpc }`; the construct OWNS its `AllowedCidr`
 * CfnParameter and callers must NOT declare their own).
 */
export interface AllowedCidrSecurityGroupProps {
  /** VPC the SG lives in. */
  readonly vpc: ec2.IVpc;
  /** TCP ports opened to the allowlisted CIDR. Default [443]. */
  readonly ports?: number[];
  /** CFN parameter logical id (console-typeable). Default 'AllowedCidr'. */
  readonly parameterName?: string;
  /** Default CIDR. Default '0.0.0.0/32' = deny-all (matches no address). */
  readonly defaultCidr?: string;
  /** SG description. */
  readonly description?: string;
  /** Allow all egress. Default false (locked-down baseline). */
  readonly allowAllOutbound?: boolean;
}

/**
 * Always-on AUTH BASELINE: a security group whose ingress is gated by a console-rotatable
 * `AllowedCidr` CloudFormation parameter (default deny-all `0.0.0.0/32`).
 *
 * Uses an L1 `CfnSecurityGroupIngress` with the `CfnParameter` token, so the CIDR can change
 * at deploy time; the parameter defaults to `0.0.0.0/32`, has an IPv4 `allowedPattern`, and keeps
 * the logical id `AllowedCidr`.
 *
 * Rotate-without-redeploy: operator opens the deployed stack in the CFN console → Update →
 * "Use existing template" → edit the `AllowedCidr` parameter → Update. The SG rule updates
 * in seconds; no `cdk deploy`.
 */
export class AllowedCidrSecurityGroup extends Construct {
  /** Attach this to the ALB / VPC endpoint / EC2 instance you want to gate. */
  public readonly securityGroup: ec2.SecurityGroup;
  /** The console-rotatable CFN parameter (logical id = parameterName ?? 'AllowedCidr'). */
  public readonly allowedCidrParam: cdk.CfnParameter;

  constructor(scope: Construct, id: string, props: AllowedCidrSecurityGroupProps) {
    super(scope, id);

    this.allowedCidrParam = new cdk.CfnParameter(this, 'AllowedCidrParam', {
      type: 'String',
      default: props.defaultCidr ?? '0.0.0.0/32',
      // IPv4 CIDR shape; defers concrete validation to deploy time
      //.
      allowedPattern: '^([0-9]{1,3}\\.){3}[0-9]{1,3}/[0-9]{1,2}$',
      // ASCII only (CFN/EC2 reject non-ASCII in some description fields).
      description:
        'IPv4 CIDR allowed to reach this demo. Default 0.0.0.0/32 blocks everyone - ' +
        'edit this parameter in the CloudFormation console (Update -> use existing ' +
        'template -> edit AllowedCidr) to allowlist your IP, e.g. 203.0.113.42/32. ' +
        'No code change or cdk deploy needed.',
    });
    // Stable, human-typeable logical id in the CFN console.
    this.allowedCidrParam.overrideLogicalId(props.parameterName ?? 'AllowedCidr');

    this.securityGroup = new ec2.SecurityGroup(this, 'Sg', {
      vpc: props.vpc,
      // ASCII only: EC2 rejects non-ASCII in GroupDescription ("Character sets
      // beyond ASCII are not supported"), found on a live deploy.
      description: props.description ?? 'AllowedCidr baseline - default deny-all',
      allowAllOutbound: props.allowAllOutbound ?? false,
    });

    for (const port of props.ports ?? [443]) {
      // L1 CfnSecurityGroupIngress — NOT ec2.Peer.ipv4(). The L2 helper validates the
      // CIDR at SYNTH time and rejects the CfnParameter token; the L1 resource defers
      // validation to DEPLOY time against the concrete value.
      //
      new ec2.CfnSecurityGroupIngress(this, `Ingress${port}`, {
        groupId: this.securityGroup.securityGroupId,
        ipProtocol: 'tcp',
        fromPort: port,
        toPort: port,
        cidrIp: this.allowedCidrParam.valueAsString,
        description: `Allow tcp/${port} from AllowedCidr`,
      });
    }
  }
}
