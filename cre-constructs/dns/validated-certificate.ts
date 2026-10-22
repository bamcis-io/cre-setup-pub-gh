import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

/**
 * The fixed name of the cross-account validator's IAM role: `<project>-acm-<region>`. The zone
 * account's DNS role trusts exactly this ARN (`setup/dns.sh`), so the name must be predictable,
 * and it is the only role in the app account that can write validation records there.
 */
export function certValidatorRoleName(projectName: string, region: string): string {
  return `${projectName}-acm-${region}`;
}

export interface ValidatedCertificateProps {
  /**
   * The project name (`$PROJECT_NAME`, `app.stackId()`). Names the validator role
   * `<project>-acm-<region>`, which the zone account trusts by exact ARN.
   */
  readonly projectName: string;
  /**
   * The demo's domain (`DOMAIN`, e.g. `new-builder-test.alias.people.aws.dev`; usually a
   * CfnParameter token). By default the certificate is the wildcard `*.<domain>`, so every
   * hostname the demo adds directly under it (`app.`, `api.`, `ui.`) is covered by one
   * certificate. The wildcard does NOT cover `<domain>` itself or deeper names (`a.b.<domain>`).
   */
  readonly domain: string;
  /**
   * Override the names on the certificate (first = primary). Default `['*.<domain>']`. Every name
   * must be in the hosted zone. Example adding the apex: `[`*.${d}`, d]`.
   */
  readonly names?: string[];
  /** The hosted zone the validation records go into (usually a CfnParameter token). */
  readonly hostedZoneId: string;
  /**
   * The zone account's DNS role (usually a CfnParameter fed from `DNS_ROLE_ARN`). Empty string =
   * the zone is in this account. Decided at deploy time by a CloudFormation Condition, because the
   * value is a protected CI variable that MR builds never see.
   */
  readonly dnsRoleArn: string;
}

/**
 * An ACM certificate validated by DNS with no manual step, whether the hosted zone is in this
 * account or another one.
 *
 *   - Same account (`dnsRoleArn` empty): CloudFormation's native `DomainValidationOptions` with
 *     `HostedZoneId`; CloudFormation writes the records itself.
 *   - Other account: a custom resource, created in parallel with the certificate, assumes
 *     `dnsRoleArn` and UPSERTs the validation CNAMEs into the zone while CloudFormation waits for
 *     issuance. Its Lambda runs as a role with a fixed name, `<project>-acm-<region>`, and the DNS
 *     role trusts exactly that ARN (`setup/dns.sh`), not every role the project's stacks create.
 *
 * Because the role name is fixed, an app can hold **one** ValidatedCertificate per region (a second
 * would collide at deploy time; it's refused at synth). The default wildcard is meant to make one
 * enough; otherwise list every name for the region in `names`.
 *
 * Expose `certificateArn` to listeners/distributions as usual.
 */
export class ValidatedCertificate extends Construct {
  public readonly certificate: acm.ICertificate;
  public readonly certificateArn: string;

  constructor(scope: Construct, id: string, props: ValidatedCertificateProps) {
    super(scope, id);
    const names = props.names && props.names.length > 0 ? props.names : [`*.${props.domain}`];
    const [primary, ...sans] = names;
    const sameAccount = new cdk.CfnCondition(this, 'ZoneInThisAccount', {
      expression: cdk.Fn.conditionEquals(props.dnsRoleArn, ''),
    });
    const crossAccount = new cdk.CfnCondition(this, 'ZoneInOtherAccount', {
      expression: cdk.Fn.conditionNot(sameAccount),
    });

    const cert = new acm.CfnCertificate(this, 'Certificate', {
      domainName: primary,
      subjectAlternativeNames: sans.length > 0 ? sans : undefined,
      validationMethod: 'DNS',
      domainValidationOptions: cdk.Fn.conditionIf(
        sameAccount.logicalId,
        names.map((n) => ({ DomainName: n, HostedZoneId: props.hostedZoneId })),
        Aws.NO_VALUE,
      ) as unknown as acm.CfnCertificate.DomainValidationOptionProperty[],
    });
    this.certificateArn = cert.ref;
    this.certificate = acm.Certificate.fromCertificateArn(this, 'Imported', cert.ref);

    // The deploy region (AWS::Region): the stack is environment-agnostic, so the role name is
    // resolved by CloudFormation. Length is checked against the longest region name.
    claimStack(this);
    const roleName = certValidatorRoleName(props.projectName, Aws.REGION);
    const worst = certValidatorRoleName(props.projectName, LONGEST_REGION_NAME);
    if (!cdk.Token.isUnresolved(props.projectName) && worst.length > 64) {
      throw new Error(`ValidatedCertificate: validator role name '${worst}' (longest region) is longer than IAM's 64 characters; shorten the project name.`);
    }
    const role = new iam.Role(this, 'ValidatorRole', {
      roleName,
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
      description: 'Writes ACM validation records into the hosted zone in the DNS account',
    });

    const fn = new lambda.Function(this, 'Validator', {
      role,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, 'cert-validator')),
      timeout: cdk.Duration.minutes(14),
      memorySize: 256,
      description: 'Writes ACM DNS validation records into another account\'s hosted zone',
    });
    fn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['acm:ListCertificates', 'acm:DescribeCertificate'],
      resources: ['*'],
    }));
    fn.addToRolePolicy(new iam.PolicyStatement({ actions: ['sts:AssumeRole'], resources: [props.dnsRoleArn] }));
    const provider = new cr.Provider(this, 'Provider', { onEventHandler: fn });

    const validation = new cdk.CustomResource(this, 'Validation', {
      serviceToken: provider.serviceToken,
      resourceType: 'Custom::CrossAccountCertValidation',
      properties: {
        DomainName: primary,
        SubjectAlternativeNames: sans,
        HostedZoneId: props.hostedZoneId,
        DnsRoleArn: props.dnsRoleArn,
      },
    });
    // Only the cross-account path deploys the custom resource, its Lambda, role and provider.
    for (const child of this.node.findAll()) {
      if (child instanceof cdk.CfnResource && child !== cert) child.cfnOptions.condition = crossAccount;
    }
    void validation;
  }
}

// One cross-account validator role per (project, region): its name is fixed, so a second
// certificate in the same stack would fail at deploy time with "already exists". Refuse it at
// synth. Two stacks deployed to the same region can't be seen here: their region is a deploy
// setting, so that case fails at deploy time ("already exists").
const claimed = new WeakMap<cdk.Stack, string>();
function claimStack(scope: Construct): void {
  const stack = cdk.Stack.of(scope);
  const prior = claimed.get(stack);
  if (prior) {
    throw new Error(`ValidatedCertificate: ${scope.node.path} is a second certificate in ${stack.stackName} (first: ${prior}). ` +
      'The validator role name is fixed per project and region; put all of the region\'s names on one certificate ' +
      '(the default wildcard, or `names`).');
  }
  claimed.set(stack, scope.node.path);
}

/** Longest current AWS region name (`ap-southeast-5`, `us-gov-west-1` are 14/13), plus margin. */
const LONGEST_REGION_NAME = 'xx-xxxxxxxxx-15';
