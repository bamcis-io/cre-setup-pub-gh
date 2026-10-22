/**
 * Access-control families. Each is vendored on its own (`npx projen constructs:add auth/<name>`),
 * so a demo carries only the mechanism it uses:
 *
 *   - `allowed-cidr`       network-source gate (AllowedCidrSecurityGroup)
 *   - `cloudfront-signer`  Midway + bindle at CloudFront (DistributionSigner)
 *   - `federate-alb`       Midway via Amazon Federate OIDC at an ALB (FederateAlbAuth)
 *   - `cognito`            Cognito user pools (ALB or HTTP API)
 */
export * from './allowed-cidr/index.js';
export * from './cloudfront-signer/index.js';
export * from './federate-alb/index.js';
export * from './cognito/index.js';
