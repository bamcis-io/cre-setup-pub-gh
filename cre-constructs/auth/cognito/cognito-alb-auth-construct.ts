import * as cdk from 'aws-cdk-lib';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as actions from 'aws-cdk-lib/aws-elasticloadbalancingv2-actions';
import { Construct } from 'constructs';
import { CognitoAuthConstruct } from './cognito-auth-construct.js';

/**
 * Props for {@link CognitoAlbAuthConstruct}.
 */
export interface CognitoAlbAuthProps {
  /**
   * Shared Cognito construct — MUST have been created with `generateClientSecret: true`
   * (confidential client) AND `hostedUiDomainPrefix` set (the ALB authenticate-cognito
   * flow needs the hosted UI).
   */
  readonly auth: CognitoAuthConstruct;
  /** The HTTPS listener (port 443) to attach the auth action to. */
  readonly httpsListener: elbv2.ApplicationListener;
  /** Action after successful auth (e.g. `elbv2.ListenerAction.forward([targetGroup])`). */
  readonly nextAction: elbv2.ListenerAction;
  /** Only authenticate matching paths; default = whole-listener default action. */
  readonly conditions?: elbv2.ListenerCondition[];
  /** Rule priority (required when `conditions` are set). */
  readonly priority?: number;
  /** ALB-issued auth-cookie session timeout. Default 7 days. */
  readonly sessionTimeout?: cdk.Duration;
}

/**
 * Variant A — ALB `authenticate-cognito` listener action.
 *
 * AWS-native: the ALB performs the OIDC handshake against the Cognito hosted UI; the
 * target never sees unauthenticated traffic, so there is NO app-side auth code. Requires
 * an HTTPS/443 listener, a CONFIDENTIAL client (`generateClientSecret: true`) and a
 * hosted-UI domain. Designed fresh — no working precedent existed.
 *
 * Keep {@link AllowedCidrSecurityGroup} on the ALB as defense-in-depth — Cognito
 * authenticates *users*; the SG limits *network reachability*.
 *
 * ⚠️ CALLBACK-URL CASE GOTCHA (verified in a live login test). The `auth` construct's
 * `callbackUrls` MUST exactly match the `redirect_uri` the ALB sends to Cognito, which is
 * `https://<alb-host>/oauth2/idpresponse` with the host **lowercased by the browser**. An
 * auto-generated ALB name is MIXED-CASE, so `loadBalancerDnsName` is mixed-case and the
 * registered callback won't match the lowercase runtime `redirect_uri` → Cognito returns
 * "Client is not enabled for OAuth2.0 flows". CDK can't `.toLowerCase()` the DNS token, so:
 *   - pin an explicit ALL-LOWERCASE `loadBalancerName` on the ALB (recommended), OR
 *   - register both cases in `callbackUrls`/`logoutUrls`.
 * Then build the callback from that lowercase DNS: `https://${alb.loadBalancerDnsName}/oauth2/idpresponse`.
 */
export class CognitoAlbAuthConstruct extends Construct {
  constructor(scope: Construct, id: string, props: CognitoAlbAuthProps) {
    super(scope, id);

    if (!props.auth.userPoolDomain) {
      throw new Error(
        'CognitoAlbAuthConstruct requires CognitoAuthConstruct to be created with ' +
          'hostedUiDomainPrefix (ALB authenticate-cognito needs the hosted UI).',
      );
    }
    // Both fail only at deploy/login time otherwise: ALB authenticate-cognito rejects a
    // public client, and with no callbackUrls CDK registers its placeholder
    // https://example.com, so every login bounces with a redirect_uri mismatch.
    if (!props.auth.generateClientSecret) {
      throw new Error(
        'CognitoAlbAuthConstruct requires CognitoAuthConstruct to be created with ' +
          'generateClientSecret: true (ALB authenticate-cognito needs a confidential client).',
      );
    }
    if (!props.auth.callbackUrls?.length) {
      throw new Error(
        'CognitoAlbAuthConstruct requires CognitoAuthConstruct callbackUrls set to ' +
          'https://<lowercase-alb-host>/oauth2/idpresponse (otherwise CDK registers https://example.com).',
      );
    }

    const authAction = new actions.AuthenticateCognitoAction({
      userPool: props.auth.userPool,
      userPoolClient: props.auth.userPoolClient,
      userPoolDomain: props.auth.userPoolDomain,
      sessionTimeout: props.sessionTimeout ?? cdk.Duration.days(7),
      next: props.nextAction,
    });

    if (props.conditions) {
      if (props.priority === undefined) {
        throw new Error(
          'CognitoAlbAuthConstruct: priority is required when conditions are set.',
        );
      }
      props.httpsListener.addAction(`${id}Action`, {
        priority: props.priority,
        conditions: props.conditions,
        action: authAction,
      });
    } else {
      props.httpsListener.addAction(`${id}Default`, { action: authAction });
    }
  }
}
