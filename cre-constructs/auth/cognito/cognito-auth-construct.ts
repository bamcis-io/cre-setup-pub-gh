import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import { OidcFederationProps } from './oidc-federation-props.js';

/**
 * Props for {@link CognitoAuthConstruct}.
 *
 * The shared user-pool half of the Cognito pattern. Both front-end variants —
 * {@link CognitoAlbAuthConstruct} (confidential client + hosted-UI domain) and
 * {@link CognitoApiGwAuthConstruct} (public SPA client) — consume the same construct and
 * diverge only at the client config.
 */
export interface CognitoAuthProps {
  /** Allow public self-signup. Demos default false (admin-created users). */
  readonly selfSignUpEnabled?: boolean;
  /**
   * OAuth callback URLs. REQUIRED for ALB authenticate-cognito / hosted UI,
   * e.g. ['https://d123.cloudfront.net/oauth2/idpresponse'].
   */
  readonly callbackUrls?: string[];
  /** OAuth logout URLs (hosted UI). */
  readonly logoutUrls?: string[];
  /**
   * Hosted-UI domain prefix (creates `userPool.addDomain`). REQUIRED for the ALB
   * authenticate-cognito flow AND for any future Midway federation; OPTIONAL for the
   * APIGW JWT flow. Cognito domain prefixes are GLOBALLY UNIQUE per region — pick a
   * collision-resistant value.
   */
  readonly hostedUiDomainPrefix?: string;
  /**
   * Generate a client secret (CONFIDENTIAL client). MUST be true for ALB
   * authenticate-cognito; MUST be false (public SPA client) for the APIGW JWT flow.
   * Default false.
   */
  readonly generateClientSecret?: boolean;
  /** OAuth scopes. Default [OPENID, EMAIL, PROFILE]. */
  readonly oAuthScopes?: cognito.OAuthScope[];
  /** OAuth grant flows. Default { authorizationCodeGrant: true } (hosted UI). */
  readonly oAuthFlows?: cognito.OAuthFlows;
  /** RemovalPolicy. Demos default DESTROY. */
  readonly removalPolicy?: cdk.RemovalPolicy;
  /**
   * MIDWAY / OIDC EXTENSION POINT — documented stub, NOT implemented in v1
   * (see {@link OidcFederationProps}). Undefined in v1 → pure no-op (no OIDC IdP).
   */
  readonly oidcFederation?: OidcFederationProps;
}

/**
 * Shared Cognito user pool + app client + optional hosted-UI domain + documented OIDC
 * federation no-op seam.
 *
 * Exposes `userPool` + `userPoolClient`, with a password policy, email sign-in and DESTROY
 * removal, and uses the current threat-protection API (verified against aws-cdk-lib 2.248.0):
 * {@link cognito.FeaturePlan} + {@link cognito.StandardThreatProtectionMode}.
 */
export class CognitoAuthConstruct extends Construct {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  /** Present iff `hostedUiDomainPrefix` was provided. */
  public readonly userPoolDomain?: cognito.UserPoolDomain;
  /** Whether the client is confidential (has a secret). Read by the ALB variant's checks. */
  public readonly generateClientSecret: boolean;
  /** The registered OAuth callback URLs, as passed in (undefined if none). */
  public readonly callbackUrls?: string[];

  constructor(scope: Construct, id: string, props: CognitoAuthProps = {}) {
    super(scope, id);
    this.generateClientSecret = props.generateClientSecret ?? false;
    this.callbackUrls = props.callbackUrls;

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      selfSignUpEnabled: props.selfSignUpEnabled ?? false,
      autoVerify: { email: true },
      signInAliases: { email: true, username: false },
      passwordPolicy: {
        minLength: 8,
        requireDigits: true,
        requireUppercase: true,
        requireLowercase: true,
        requireSymbols: true,
      },
      // CURRENT threat-protection API (verified present in aws-cdk-lib 2.248.0):
      // replaces the deprecated advancedSecurityMode: ENFORCED. StandardThreatProtectionMode
      // only functions with FeaturePlan.PLUS.
      featurePlan: cognito.FeaturePlan.PLUS,
      standardThreatProtectionMode: cognito.StandardThreatProtectionMode.FULL_FUNCTION,
      removalPolicy: props.removalPolicy ?? cdk.RemovalPolicy.DESTROY,
    });

    if (props.hostedUiDomainPrefix) {
      this.userPoolDomain = this.userPool.addDomain('HostedUiDomain', {
        cognitoDomain: { domainPrefix: props.hostedUiDomainPrefix },
      });
    }

    const useOAuth = Boolean(props.callbackUrls || props.generateClientSecret);
    this.userPoolClient = this.userPool.addClient('Client', {
      generateSecret: props.generateClientSecret ?? false,
      authFlows: { userPassword: true, userSrp: true },
      // When NOT using the hosted-UI/OAuth flow (e.g. the APIGW JWT path: a pure SPA
      // client that does Cognito SDK auth + sends a bearer token), DISABLE OAuth
      // explicitly. Passing `oAuth: undefined` makes CDK apply its DEFAULTS (implicit
      // grant + a placeholder https://example.com callback), which we don't want on a
      // JWT-only client. disableOAuth yields AllowedOAuthFlows/CallbackURLs = none.
      disableOAuth: !useOAuth,
      oAuth: useOAuth
        ? {
            flows: props.oAuthFlows ?? { authorizationCodeGrant: true },
            scopes: props.oAuthScopes ?? [
              cognito.OAuthScope.OPENID,
              cognito.OAuthScope.EMAIL,
              cognito.OAuthScope.PROFILE,
            ],
            callbackUrls: props.callbackUrls,
            logoutUrls: props.logoutUrls,
          }
        : undefined,
      // v1: COGNITO only. The Midway seam (oidcFederation) flips this to include the OIDC
      // provider — see OidcFederationProps. Intentionally NOT consumed in v1.
      supportedIdentityProviders: [cognito.UserPoolClientIdentityProvider.COGNITO],
    });

    // props.oidcFederation is intentionally NOT consumed in v1 (documented no-op seam).

    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
    });
  }
}
