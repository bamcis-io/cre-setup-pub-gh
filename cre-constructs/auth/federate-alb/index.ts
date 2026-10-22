/**
 * Midway at an Application Load Balancer via Amazon Federate OIDC: {@link FederateAlbAuth}.
 * `app-helper/oidc-data.ts` (Node) and `app-helper/oidc_data.py` (Python) validate the
 * `x-amzn-oidc-data` header in the application; they are app code, not exported here.
 */
export * from './federate-alb-auth.js';
