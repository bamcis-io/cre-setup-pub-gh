import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { requireDeployRegion } from '../../core/cre-stack.js';
import { Construct } from 'constructs';

/**
 * CloudFrontSigner signing API version.
 */
export enum CloudFrontSignerSigningAPIVersion {
  V2 = 'v2',
}

/**
 * Regions where CloudFrontSigner can read the signing secret from Secrets Manager.
 * Mirrors CloudFrontSigner's MultiRegionSecretsManagerConfig (linked from the CFS
 * onboarding wiki, §1.2.1); a secret anywhere else fails onboarding with "not in a
 * supported region for CloudFrontSigner".
 */
export const CFS_SUPPORTED_SECRET_REGIONS: readonly string[] = [
  'us-east-1', 'us-west-2', 'eu-central-1', 'eu-west-1', 'ap-northeast-1', 'ap-southeast-2',
  'cn-north-1', 'cn-northwest-1',
];

/**
 * Properties for DistributionSigner.apply().
 */
export interface DistributionSignerProps {
  /** Bindle ID for CloudFrontSigner authorization. */
  readonly authorizerBindleId: string;

  /**
   * CloudFrontSigner service endpoint.
   * @default 'https://cloudfrontsigner.ninjas.security.a2z.com'
   */
  readonly serviceEndpoint?: string;

  /**
   * Signing API version.
   * @default CloudFrontSignerSigningAPIVersion.V2
   */
  readonly signingAPIVersion?: CloudFrontSignerSigningAPIVersion;

  /**
   * Behaviors that should NOT have viewer access restricted.
   * Every other cache behavior on the distribution (default and additional) gets the
   * trusted key group; these path patterns are excluded from it.
   * /js/cfs-handler.js and /error/* are always excluded automatically.
   */
  readonly denyListBehaviors?: string[];
}

/**
 * Applies CloudFrontSigner (Midway) authentication to a CloudFront distribution.
 *
 * Mirrors the API of the internal `@amzn/cloudfront-signer` DistributionSigner construct
 * but without the Brazil/BATS dependency.
 *
 * - Adds a trusted key group to every behavior on the distribution (default and
 *   additional, including ones added after apply()) except `denyListBehaviors`
 * - Adds an S3 origin + behaviors for /js/cfs-handler.js and /error/* (unrestricted)
 * - Adds a custom error response (403 → /error/403.html), keeping the caller's others
 * - Deploys the 403.html and cfs-handler.js files to S3
 * - Provisions the RSA key pair via custom resource (private key never leaves Lambda)
 *
 * Usage:
 * ```ts
 * const distribution = new cloudfront.Distribution(this, 'Dist', { ... });
 *
 * DistributionSigner.apply(distribution, {
 *   authorizerBindleId: 'amzn1.bindle.resource.xxxxx',
 * });
 * ```
 */
export class DistributionSigner extends Construct {
  /**
   * Apply CloudFrontSigner authentication to a CloudFront distribution.
   */
  public static override apply(
    distribution: cloudfront.Distribution,
    props: DistributionSignerProps,
  ): DistributionSigner {
    const scope = distribution.node.scope as Construct;
    // Named after the distribution, not a fixed id, so one scope can sign several
    // distributions (e.g. an app UI and a Locust UI in the same stack). A fixed
    // 'DistributionSigner' id threw "There is already a Construct with name ..." on
    // the second apply().
    return new DistributionSigner(scope, `DistributionSigner${distribution.node.id}`, distribution, props);
  }

  /** ARN of the Secrets Manager secret holding the signing key pair. */
  public readonly secretArn: string;

  private constructor(scope: Construct, id: string, distribution: cloudfront.Distribution, props: DistributionSignerProps) {
    super(scope, id);

    // CFS can only read the secret in a fixed set of regions. The stack is environment-agnostic,
    // so the check is a CloudFormation Rule on AWS::Region: a deploy anywhere else fails before
    // any resource is created, instead of surfacing later as an onboarding error.
    requireDeployRegion(this, 'CfsSupportedSecretRegion', CFS_SUPPORTED_SECRET_REGIONS,
      'CloudFrontSigner reads the signing secret only there (us-east-1 recommended; a VPC origin can front an origin elsewhere)');

    // Per-stack-instance id: the UUID at the end of the stack ARN
    // (arn:...:stack/<name>/<uuid>). It changes on every destroy + redeploy, which is what
    // keeps account-unique names below from colliding with a predecessor's leftovers.
    const stackUuid = cdk.Fn.select(2, cdk.Fn.split('/', Aws.STACK_ID));

    const serviceEndpoint = props.serviceEndpoint ?? 'https://cloudfrontsigner.ninjas.security.a2z.com';
    const apiVersion = props.signingAPIVersion ?? CloudFrontSignerSigningAPIVersion.V2;
    const authorizeEndpoint = `${serviceEndpoint}/${apiVersion}/authorize`;

    // --- KMS key for Secrets Manager encryption ---
    const encryptionKey = new kms.Key(this, 'EncryptionKey', {
      alias: `CFSEncryptionKey-${cdk.Names.uniqueId(this)}`,
      description: 'CloudFrontSigner encryption key for signing secret',
      enableKeyRotation: true,
      // DESTROY (not the kms.Key default of RETAIN) so a `cdk destroy` schedules the
      // key for deletion instead of orphaning it. Orphaned CMKs were the root cause of
      // the "Access to KMS is not allowed" drift: each redeploy minted a fresh CMK
      // while the retained signing secret stayed bound to a dead one. The 7-day pending
      // window keeps the teardown reversible.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      pendingWindow: cdk.Duration.days(7),
    });
    // Exactly the statement the CFS onboarding wiki prescribes. Deliberately not
    // trimmed to Decrypt and not narrowed with aws:SourceAccount/SourceArn: CFS documents
    // neither, and a condition its requests don't carry would silently break every read.
    // The binding that matters (secret ↔ Bindle ↔ domain) is enforced by CFS at onboarding.
    encryptionKey.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowCloudFrontSignerService',
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('cloudfrontsigner.aws.internal')],
      actions: ['kms:Encrypt', 'kms:Decrypt', 'kms:ReEncrypt*', 'kms:GenerateDataKey*', 'kms:DescribeKey'],
      resources: ['*'],
    }));

    // --- Signing secret (native CFN resource) ---
    // A first-class Secrets Manager resource: created and destroyed with the stack and
    // encrypted with the stack's CMK. Because the secret and the CMK now share one
    // lifecycle, a retained secret can never outlive its key — the "Access to KMS is
    // not allowed" drift is structurally impossible. The provisioner Lambda only
    // *writes* the key material into it (put_secret_value); CFN owns its lifecycle. The
    // private key is generated inside the Lambda, so it never appears in the template.
    //
    // The name MUST start with `CFSSigningKey` — the CFS onboarding wiki: "otherwise
    // onboarding will fail". It must also be unique per stack instance: the handler that
    // preceded this (4afc8ab) created `CFSSigningKey-<uniqueId>` itself and never deleted it,
    // and a destroyed secret sits in its recovery window, so any name a redeploy can repeat
    // fails with "already exists". The stack UUID suffix rules out both.
    const signingSecret = new secretsmanager.Secret(this, 'SigningSecret', {
      secretName: cdk.Fn.join('-', ['CFSSigningKey', cdk.Names.uniqueId(this), stackUuid]),
      description: 'CloudFrontSigner RSA key pair for cookie signing',
      encryptionKey,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.secretArn = signingSecret.secretArn;

    // Allow the CloudFrontSigner service (cross-account) to read the secret.
    signingSecret.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'AllowCloudFrontSignerService',
      effect: iam.Effect.ALLOW,
      principals: [new iam.ServicePrincipal('cloudfrontsigner.aws.internal')],
      actions: ['secretsmanager:GetSecretValue', 'secretsmanager:DescribeSecret'],
      resources: ['*'],
    }));

    // --- Custom resource Lambda ---
    const provisionerFn = new lambda.Function(this, 'KeyProvisionerFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, 'cfs-key-provisioner')),
      timeout: cdk.Duration.minutes(5),
      memorySize: 256,
      logGroup: new logs.LogGroup(this, 'KeyProvisionerLogGroup', {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    provisionerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: [
        'cloudfront:CreatePublicKey', 'cloudfront:ListPublicKeys',
        'cloudfront:GetPublicKey', 'cloudfront:DeletePublicKey',
      ],
      resources: ['*'],
    }));
    provisionerFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['kms:GenerateDataKeyPair', 'kms:Encrypt', 'kms:Decrypt', 'kms:GenerateDataKey'],
      resources: [encryptionKey.keyArn],
    }));
    // The provisioner writes the generated key material into the CFN-managed secret.
    signingSecret.grantWrite(provisionerFn);

    const keyResource = new cdk.CustomResource(this, 'KeyResource', {
      serviceToken: provisionerFn.functionArn,
      properties: {
        KmsKeyId: encryptionKey.keyId,
        ResourceNameSuffix: cdk.Names.uniqueId(this),
        // ARN of the native secret the provisioner writes the key material into.
        // Passing it also makes the custom resource depend on the secret's creation.
        SecretArn: signingSecret.secretArn,
        // Bump to force the provisioner to regenerate the key pair on an already-
        // deployed stack — the supported way to rotate the signing key pair in place.
        KeyPairVersion: '2',
      },
    });

    const publicKeyId = keyResource.getAttString('PublicKeyId');

    // --- CloudFront Key Group ---
    const cfnKeyGroup = new cloudfront.CfnKeyGroup(this, 'KeyGroup', {
      keyGroupConfig: {
        // Key-group names are unique per ACCOUNT (CloudFront is global), and uniqueId is
        // only a hash of the construct path, so the same stack in two regions or two
        // builders sharing an account collided. Max 128 chars of [A-Za-z0-9_-].
        name: cdk.Fn.join('-', [
          'CFSKeyGroup',
          cdk.Names.uniqueResourceName(this, { maxLength: 64 }),
          stackUuid,
        ]),
        items: [publicKeyId],
        comment: 'CloudFrontSigner trusted key group',
      },
    });

    // --- Secrets Manager resource policy ---
    // (Now attached directly to the native secret via addToResourcePolicy above.)

    // --- S3 bucket + origin for CFS static files ---
    const filesBucket = new s3.Bucket(this, 'FilesBucket', {
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
    });

    const filesOrigin = origins.S3BucketOrigin.withOriginAccessControl(filesBucket);

    // --- Deploy CFS static files ---
    const html403 = [
      '<!DOCTYPE html>',
      '<html>',
      '<head>',
      '    <script src="/js/cfs-handler.js"></script>',
      '</head>',
      '<body>',
      '<p>Redirecting you for Auth</p>',
      `<div style="display:none" id="bindlediv">${props.authorizerBindleId}</div>`,
      `<div style="display:none" id="secretArn">${this.secretArn}</div>`,
      '</body>',
      '</html>',
    ].join('\n');

    new s3deploy.BucketDeployment(this, 'FilesDeploy', {
      sources: [
        s3deploy.Source.data('error/403.html', html403),
        s3deploy.Source.data('js/cfs-handler.js', this.renderCfsHandlerJs(authorizeEndpoint)),
      ],
      destinationBucket: filesBucket,
      // Invalidate on every deploy: CloudFront caches these up to 24h, and a stale 403
      // page keeps pointing CFS at the previous secret ARN after a redeploy.
      distribution,
      distributionPaths: ['/error/*', '/js/cfs-handler.js'],
    });

    // --- Mutate the distribution ---
    // Add unrestricted behaviors for CFS static files
    distribution.addBehavior('/js/cfs-handler.js', filesOrigin, {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });
    distribution.addBehavior('/error/*', filesOrigin, {
      viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
    });

    // Trusted key group + error response via the L1 escape hatch. Deferred to an aspect
    // so it sees the distribution's final behaviors and error responses — including any
    // the caller adds after apply() — instead of a snapshot taken here.
    const cfnDistribution = distribution.node.defaultChild as cloudfront.CfnDistribution;
    const unrestricted = new Set(['/js/cfs-handler.js', '/error/*', ...(props.denyListBehaviors ?? [])]);
    cdk.Aspects.of(distribution).add({
      visit: (node) => {
        if (node === cfnDistribution) {
          this.restrictBehaviors(cfnDistribution, cfnKeyGroup.attrId, unrestricted);
          this.addErrorResponse(cfnDistribution);
        }
      },
    });
  }

  /** Attach the key group to every behavior whose path pattern is not in `unrestricted`. */
  private restrictBehaviors(cfn: cloudfront.CfnDistribution, keyGroupId: string, unrestricted: Set<string>): void {
    // Overrides deep-merge into the *resolved* template, so indexing into the rendered
    // CacheBehaviors array is safe.
    const config = cdk.Stack.of(cfn).resolve(cfn.distributionConfig) as {
      defaultCacheBehavior?: { trustedKeyGroups?: unknown[] };
      cacheBehaviors?: { pathPattern: string; trustedKeyGroups?: unknown[] }[];
    };
    // Keep any key groups the caller attached themselves; add ours alongside.
    cfn.addPropertyOverride(
      'DistributionConfig.DefaultCacheBehavior.TrustedKeyGroups',
      [...(config.defaultCacheBehavior?.trustedKeyGroups ?? []), keyGroupId],
    );
    const behaviors = config.cacheBehaviors ?? [];
    behaviors.forEach((b, i) => {
      if (unrestricted.has(b.pathPattern)) return;
      cfn.addPropertyOverride(
        `DistributionConfig.CacheBehaviors.${i}.TrustedKeyGroups`,
        [...(b.trustedKeyGroups ?? []), keyGroupId],
      );
    });
    // A typo in denyListBehaviors would silently leave that path restricted; say so.
    const patterns = new Set(behaviors.map((b) => b.pathPattern));
    for (const p of unrestricted) {
      if (!patterns.has(p)) {
        cdk.Annotations.of(this).addWarningV2(
          '@cre-demo-template/edge:denyListBehaviorNotFound',
          `denyListBehaviors entry '${p}' matches no behavior on the distribution`,
        );
      }
    }
  }

  /**
   * Route 403s to the CFS redirect page while keeping the caller's other error responses.
   * A caller-supplied 403 response is replaced: the redirect is how an unsigned viewer
   * reaches Midway, so the construct cannot work without it.
   */
  private addErrorResponse(cfn: cloudfront.CfnDistribution): void {
    const config = cdk.Stack.of(cfn).resolve(cfn.distributionConfig) as {
      customErrorResponses?: { errorCode: number }[];
    };
    const existing = config.customErrorResponses ?? [];
    const ours = { ErrorCode: 403, ResponseCode: 403, ResponsePagePath: '/error/403.html' };
    if (existing.length === 0) {
      cfn.addPropertyOverride('DistributionConfig.CustomErrorResponses', [ours]);
      return;
    }
    const i403 = existing.findIndex((r) => r.errorCode === 403);
    if (i403 >= 0) {
      cdk.Annotations.of(this).addWarningV2(
        '@cre-demo-template/edge:errorResponse403Replaced',
        "the distribution's 403 error response is replaced by the CloudFrontSigner redirect page",
      );
    }
    // Overrides deep-merge, so clear the caller's TTL rather than inherit it on our 403.
    cfn.addPropertyOverride(
      `DistributionConfig.CustomErrorResponses.${i403 >= 0 ? i403 : existing.length}`,
      { ...ours, ErrorCachingMinTTL: undefined },
    );
  }

  private renderCfsHandlerJs(authorizeEndpoint: string): string {
    const templatePath = path.join(__dirname, 'static', 'cfs', 'js', 'cfs-handler.js');
    const template = fs.readFileSync(templatePath, 'utf8') as string;
    return template.replace(
      'https://cloudfrontsigner.ninjas.security.a2z.com/v2/authorize',
      authorizeEndpoint,
    );
  }
}
