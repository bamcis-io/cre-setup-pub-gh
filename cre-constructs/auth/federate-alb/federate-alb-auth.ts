import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

/**
 * Amazon Federate OIDC endpoints (the Federate team's ALB integration page:
 * https://w.amazon.com/bin/view/AmazonFederate/help/ALB/).
 */
export const FEDERATE_ENDPOINTS = {
  prod: {
    issuer: 'https://idp.federate.amazon.com',
    authorizationEndpoint: 'https://idp.federate.amazon.com/api/oauth2/v1/authorize',
    tokenEndpoint: 'https://idp.federate.amazon.com/api/oauth2/v2/token',
    userInfoEndpoint: 'https://idp.federate.amazon.com/api/oauth2/v1/userinfo',
  },
  integ: {
    issuer: 'https://idp-integ.federate.amazon.com',
    authorizationEndpoint: 'https://idp-integ.federate.amazon.com/api/oauth2/v1/authorize',
    tokenEndpoint: 'https://idp-integ.federate.amazon.com/api/oauth2/v2/token',
    userInfoEndpoint: 'https://idp-integ.federate.amazon.com/api/oauth2/v1/userinfo',
  },
} as const;

/**
 * Domains Midway's internal allowlist (the `MidwayAllowlist` package) accepts for redirect URIs on
 * the auto-approved "Midway-Protected Internal Service" Federate template. Not exhaustive: a
 * hostname elsewhere gets a synth warning, because its Federate profile needs a security review.
 */
const AUTO_APPROVED_SUFFIXES = ['.aws.dev', '.amazon.dev', '.a2z.com', '.amazon.com'];

export interface FederateAlbAuthProps {
  /** An HTTPS listener (ALB authentication requires HTTPS). */
  readonly listener: elbv2.ApplicationListener;
  /** The listener's load balancer, so its egress to Federate can be opened. */
  readonly loadBalancer: elbv2.ApplicationLoadBalancer;
  /**
   * The hostname users browse to, lowercase (e.g. `demo.alias.people.aws.dev`). The Federate
   * service profile's redirect URI is `https://<hostname>/oauth2/idpresponse`.
   */
  readonly hostname: string;
  /** What to do once authenticated (usually `ListenerAction.forward([targetGroup])`). */
  readonly next: elbv2.ListenerAction;
  /** The Federate service profile's client ID. */
  readonly clientId: string;
  /**
   * The profile's client secret, in Secrets Manager. Never a literal: the Federate page says
   * never to hardcode it.
   */
  readonly clientSecret: secretsmanager.ISecret;
  /** JSON key inside the secret, when the secret is a JSON document. @default - the whole secret string */
  readonly clientSecretJsonField?: string;
  /** @default 'prod' */
  readonly federateEnvironment?: 'prod' | 'integ';
  /** @default Duration.hours(10), the Federate page's recommendation */
  readonly sessionTimeout?: cdk.Duration;
  /**
   * `authenticate` redirects browsers to log in; `deny` returns 401 (for APIs called by an already-
   * logged-in page). @default AUTHENTICATE
   */
  readonly onUnauthenticatedRequest?: elbv2.UnauthenticatedAction;
}

/**
 * Midway authentication at an Application Load Balancer, through Amazon Federate OIDC.
 *
 * The listener's default action becomes `authenticate-oidc` against Federate, then `next`. The ALB
 * forwards the user's claims to targets in the `x-amzn-oidc-data` header (a signed JWT) — the
 * application MUST validate it (see `app-helper/oidc-data.ts` / `oidc_data.py`), and targets must
 * accept traffic only from the ALB (`allowTargetsFromLoadBalancer`), or anyone reaching a target
 * directly could forge the header.
 *
 * Federate side (once per hostname): an OIDC service profile, from the "Midway-Protected Internal
 * Service" template, with redirect URI {@link redirectUri} and **PKCE disabled** (ALB doesn't
 * support PKCE). The auto-approved template only accepts allowlisted domains such as `aws.dev`.
 *
 * Fails at synth when the listener isn't HTTPS, or the hostname isn't lowercase (browsers
 * lowercase the host in the redirect, so a mixed-case registration never matches).
 */
export class FederateAlbAuth extends Construct {
  /** Register this on the Federate service profile. */
  public readonly redirectUri: string;

  private readonly loadBalancer: elbv2.ApplicationLoadBalancer;

  constructor(scope: Construct, id: string, props: FederateAlbAuthProps) {
    super(scope, id);

    if (props.listener.protocol !== elbv2.ApplicationProtocol.HTTPS) {
      throw new Error(`FederateAlbAuth: the listener must be HTTPS (ALB OIDC authentication requires it); got ${props.listener.protocol}.`);
    }
    if (!cdk.Token.isUnresolved(props.hostname)) {
      if (props.hostname !== props.hostname.toLowerCase()) {
        throw new Error(`FederateAlbAuth: hostname '${props.hostname}' must be lowercase; browsers lowercase it in the OIDC redirect.`);
      }
      if (!AUTO_APPROVED_SUFFIXES.some((s) => props.hostname.endsWith(s))) {
        cdk.Annotations.of(this).addWarningV2(
          '@cre/auth:federateHostname',
          `'${props.hostname}' is not under a Midway-allowlisted domain (e.g. aws.dev); its Federate profile can't use the auto-approved template and needs a security review.`,
        );
      }
    }

    const ep = FEDERATE_ENDPOINTS[props.federateEnvironment ?? 'prod'];
    this.redirectUri = `https://${props.hostname}/oauth2/idpresponse`;
    this.loadBalancer = props.loadBalancer;

    props.listener.addAction('FederateAuth', {
      action: elbv2.ListenerAction.authenticateOidc({
        ...ep,
        clientId: props.clientId,
        clientSecret: props.clientSecretJsonField
          ? props.clientSecret.secretValueFromJson(props.clientSecretJsonField)
          : props.clientSecret.secretValue,
        scope: 'openid',
        sessionCookieName: 'AWSELBAuthSessionCookie',
        sessionTimeout: props.sessionTimeout ?? cdk.Duration.hours(10),
        onUnauthenticatedRequest: props.onUnauthenticatedRequest ?? elbv2.UnauthenticatedAction.AUTHENTICATE,
        next: props.next,
      }),
    });

    // The ALB calls Federate's token and userinfo endpoints itself; blocked egress shows up as an
    // HTTP 500 on login.
    props.loadBalancer.connections.allowToAnyIpv4(ec2.Port.tcp(443), 'ALB to Amazon Federate (OIDC token/userinfo)');

    new cdk.CfnOutput(this, 'FederateRedirectUri', {
      value: this.redirectUri,
      description: 'Redirect URI to register on the Federate service profile (PKCE disabled).',
    });
  }

  /**
   * Let only the load balancer reach these targets on `port`. Call it for every target, and give
   * targets no other ingress: a target reachable directly could be sent a forged
   * `x-amzn-oidc-data` header.
   */
  public allowTargetsFromLoadBalancer(port: ec2.Port, ...targets: ec2.IConnectable[]): void {
    for (const t of targets) {
      t.connections.allowFrom(this.loadBalancer, port, 'Only the Federate-authenticated ALB');
    }
  }
}
