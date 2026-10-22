import * as cognito from 'aws-cdk-lib/aws-cognito';

/**
 * MIDWAY / OIDC FEDERATION EXTENSION POINT.
 *
 * DOCUMENTED STUB — present in the interface, UNIMPLEMENTED in v1. Passing
 * {@link CognitoAuthProps.oidcFederation} has NO EFFECT in v1 (pure no-op seam): no
 * `UserPoolIdentityProviderOidc` is created and the app client's
 * `supportedIdentityProviders` stays `[COGNITO]`.
 *
 * **Why a seam and not an implementation:** the design ships Seam 1 (a Cognito user-pool
 * OIDC identity provider) because it works uniformly for BOTH the ALB
 * (`authenticate-cognito`) and APIGW (`HttpUserPoolAuthorizer`) variants — everything
 * downstream still talks to Cognito, so flipping federation on later requires ZERO rework
 * to either front-end variant. The only v1 prerequisites are already satisfied:
 *   (a) {@link CognitoAuthConstruct} creates a hosted-UI domain when asked (federation
 *       requires the hosted/managed login UI), and
 *   (b) this `oidcFederation?` prop already exists on the interface.
 *
 * **Exactly what flips on later** (inside {@link CognitoAuthConstruct}, when defined):
 * ```ts
 * const provider = new cognito.UserPoolIdentityProviderOidc(this, 'Oidc', {
 *   userPool: this.userPool,
 *   name: props.oidcFederation.name,
 *   clientId: props.oidcFederation.clientId,
 *   clientSecret: props.oidcFederation.clientSecret,
 *   issuerUrl: props.oidcFederation.issuerUrl,
 *   scopes: props.oidcFederation.scopes ?? ['openid'],
 *   attributeMapping: props.oidcFederation.attributeMapping,
 * });
 * this.userPoolClient.node.addDependency(provider);
 * // and add cognito.UserPoolClientIdentityProvider.custom(name) to supportedIdentityProviders
 * ```
 */
export interface OidcFederationProps {
  /** IdP name as it appears in Cognito, e.g. 'Midway'. */
  readonly name: string;
  /** OIDC client id issued by the IdP. */
  readonly clientId: string;
  /** OIDC client secret (in practice sourced from Secrets Manager). */
  readonly clientSecret: string;
  /** OIDC issuer URL (the IdP's discovery base). */
  readonly issuerUrl: string;
  /** OIDC scopes. Default ['openid']. */
  readonly scopes?: string[];
  /** Optional Cognito attribute mapping from the IdP claims. */
  readonly attributeMapping?: cognito.AttributeMapping;
}
