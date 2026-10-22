/**
 * Validate the `x-amzn-oidc-data` header an ALB adds after Federate (OIDC) authentication.
 *
 * Application code, not CDK: copy into the app (Node 18+, no dependencies). The Federate team's
 * ALB page says the application MUST validate this header, or anything that reaches a target
 * directly can forge a user. Checks, in order:
 *   1. the JWT has three parts and an ES256 header with `kid` and `signer`;
 *   2. `signer` equals this ALB's ARN;
 *   3. the signature verifies against the ALB public key for `kid`, fetched from the ALB key
 *      endpoint for the ALB's own partition and region, both read from `albArn` (cached per kid):
 *      `https://public-keys.auth.elb.<region>.amazonaws.com/<kid>` in `aws`, the documented S3
 *      endpoint in `aws-us-gov`; any other partition needs `keyEndpoint`;
 *   4. `exp` (seconds, in the header) has not passed.
 * Returns the claims (`sub`, `email`, ... as configured on the Federate profile).
 */
import { createPublicKey, verify } from 'crypto';

export interface OidcDataOptions {
  /** The ALB's ARN (the only signer accepted). */
  readonly albArn: string;
  /**
   * Base URL of the ALB public-key endpoint (the key id is appended after a `/`). Default: derived
   * from `albArn`'s partition and region (`aws`, `aws-us-gov`). Required in other partitions,
   * whose endpoint AWS doesn't document.
   */
  readonly keyEndpoint?: string;
  /** Override the key fetcher (tests). Returns the PEM public key for `kid`. */
  readonly fetchKey?: (url: string) => Promise<string>;
  /** Clock skew allowance in seconds. @default 0 */
  readonly leewaySeconds?: number;
  /** Current time in seconds (tests). */
  readonly now?: () => number;
}

export class OidcDataError extends Error {}

/**
 * The ALB public-key endpoint for the ALB's partition and region, from its ARN
 * (`arn:<partition>:elasticloadbalancing:<region>:...`). Endpoints per the ALB authentication docs.
 */
export function albKeyEndpoint(albArn: string): string {
  const [, partition, service, region] = albArn.split(':');
  if (service !== 'elasticloadbalancing' || !region) throw new OidcDataError(`not a load balancer ARN: ${albArn}`);
  if (partition === 'aws') return `https://public-keys.auth.elb.${region}.amazonaws.com`;
  if (partition === 'aws-us-gov') return `https://s3-${region}.amazonaws.com/aws-elb-public-keys-prod-${region}`;
  throw new OidcDataError(`no documented ALB public-key endpoint for partition ${partition}; pass keyEndpoint`);
}

const keyCache = new Map<string, string>();

/** ALB pads its base64url segments with `=`, which strict decoders reject; tolerate it. */
function b64urlJson(part: string): Record<string, unknown> {
  const s = part.replace(/=+$/, '').replace(/-/g, '+').replace(/_/g, '/');
  return JSON.parse(Buffer.from(s, 'base64').toString('utf8'));
}

async function defaultFetch(url: string): Promise<string> {
  const res = await fetch(url);
  if (!res.ok) throw new OidcDataError(`public key fetch failed: ${res.status} ${url}`);
  return res.text();
}

export async function verifyOidcData(header: string | undefined, opts: OidcDataOptions): Promise<Record<string, unknown>> {
  if (!header) throw new OidcDataError('missing x-amzn-oidc-data');
  const parts = header.split('.');
  if (parts.length !== 3) throw new OidcDataError('malformed JWT');
  const [h, p, sig] = parts;

  let jose: Record<string, unknown>;
  try {
    jose = b64urlJson(h);
  } catch {
    throw new OidcDataError('malformed JWT header');
  }
  if (jose.alg !== 'ES256') throw new OidcDataError(`unexpected alg ${String(jose.alg)}`);
  const kid = jose.kid;
  if (typeof kid !== 'string' || !/^[A-Za-z0-9-]+$/.test(kid)) throw new OidcDataError('missing or invalid kid');
  if (jose.signer !== opts.albArn) throw new OidcDataError('signer is not this load balancer');

  const url = `${opts.keyEndpoint ?? albKeyEndpoint(opts.albArn)}/${kid}`;
  let pem = keyCache.get(url);
  if (!pem) {
    pem = await (opts.fetchKey ?? defaultFetch)(url);
    keyCache.set(url, pem);
  }
  const ok = verify(
    'sha256',
    Buffer.from(`${h}.${p}`),
    { key: createPublicKey(pem), dsaEncoding: 'ieee-p1363' },
    Buffer.from(sig.replace(/=+$/, ''), 'base64url'),
  );
  if (!ok) throw new OidcDataError('bad signature');

  const now = (opts.now ?? (() => Math.floor(Date.now() / 1000)))();
  const exp = Number(jose.exp);
  if (!Number.isFinite(exp) || exp + (opts.leewaySeconds ?? 0) < now) throw new OidcDataError('token expired');

  try {
    return b64urlJson(p);
  } catch {
    throw new OidcDataError('malformed JWT payload');
  }
}

/** For tests: forget cached keys. */
export function clearOidcKeyCache(): void {
  keyCache.clear();
}
