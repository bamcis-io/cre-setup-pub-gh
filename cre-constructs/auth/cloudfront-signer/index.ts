/**
 * Midway at the edge: {@link DistributionSigner} puts a CloudFront distribution behind
 * CloudFront Signer (Midway authentication + bindle authorization), using the hardened V2 flow
 * (15-minute cookie TTL, signed values in the URL fragment, nonce/RFP request-forgery protection,
 * `SameSite=Lax`).
 */
export * from './distribution-signer.js';
export * from './edge-origin.js';
export * from './edge-stack.js';
