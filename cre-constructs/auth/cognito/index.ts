/**
 * Cognito user authentication: the shared {@link CognitoAuthConstruct} (user pool + client +
 * optional hosted-UI domain + OIDC seam) and its two front ends, {@link CognitoAlbAuthConstruct}
 * (ALB authenticate-cognito) and {@link CognitoApiGwAuthConstruct} (HTTP API JWT authorizer +
 * `/amplify-config`).
 */
export * from './oidc-federation-props.js';
export * from './cognito-auth-construct.js';
export * from './cognito-alb-auth-construct.js';
export * from './cognito-apigw-auth-construct.js';
