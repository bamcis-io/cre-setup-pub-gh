"""Validate the ``x-amzn-oidc-data`` header an ALB adds after Federate (OIDC) authentication.

Application code: vendor it into the app. Needs ``cryptography`` (ES256 verification) and nothing
else. Same checks as ``oidc-data.ts``: ES256 header with ``kid`` and ``signer``; ``signer`` equals
this ALB's ARN; signature verifies against the ALB key for ``kid`` from the key endpoint of the
ALB's own partition and region, read from ``alb_arn`` (``aws``: public-keys.auth.elb.<region>;
``aws-us-gov``: the documented S3 endpoint; other partitions need ``key_endpoint``), cached;
header ``exp`` not passed.
Returns the claims dict; raises ``OidcDataError`` otherwise.
"""
import base64
import json
import re
import time
import urllib.request
from typing import Callable, Dict, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


class OidcDataError(Exception):
    pass


_KEYS: Dict[str, bytes] = {}


def _b64(part: str) -> bytes:
    s = part.rstrip("=").replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _fetch(url: str) -> bytes:
    # Short timeout: a hanging key fetch must not outlive the request it guards.
    with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 (fixed https endpoint)
        return r.read()


def alb_key_endpoint(alb_arn: str) -> str:
    """The ALB public-key endpoint for the ALB's partition and region, from its ARN."""
    parts = alb_arn.split(":")
    if len(parts) < 4 or parts[2] != "elasticloadbalancing" or not parts[3]:
        raise OidcDataError(f"not a load balancer ARN: {alb_arn}")
    partition, region = parts[1], parts[3]
    if partition == "aws":
        return f"https://public-keys.auth.elb.{region}.amazonaws.com"
    if partition == "aws-us-gov":
        return f"https://s3-{region}.amazonaws.com/aws-elb-public-keys-prod-{region}"
    raise OidcDataError(f"no documented ALB public-key endpoint for partition {partition}; pass key_endpoint")


def verify_oidc_data(header: Optional[str], alb_arn: str,
                     fetch_key: Callable[[str], bytes] = _fetch, now: Optional[float] = None,
                     leeway: int = 0, key_endpoint: Optional[str] = None) -> dict:
    if not header:
        raise OidcDataError("missing x-amzn-oidc-data")
    parts = header.split(".")
    if len(parts) != 3:
        raise OidcDataError("malformed JWT")
    h, p, sig = parts
    try:
        jose = json.loads(_b64(h))
    except Exception as e:  # noqa: BLE001
        raise OidcDataError("malformed JWT header") from e
    if jose.get("alg") != "ES256":
        raise OidcDataError(f"unexpected alg {jose.get('alg')}")
    kid = jose.get("kid")
    if not isinstance(kid, str) or not re.fullmatch(r"[A-Za-z0-9-]+", kid):
        raise OidcDataError("missing or invalid kid")
    if jose.get("signer") != alb_arn:
        raise OidcDataError("signer is not this load balancer")
    url = f"{key_endpoint or alb_key_endpoint(alb_arn)}/{kid}"
    pem = _KEYS.get(url) or fetch_key(url)
    _KEYS[url] = pem
    key = serialization.load_pem_public_key(pem)
    raw = _b64(sig)
    if len(raw) != 64:
        raise OidcDataError("bad signature")
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as e:
        raise OidcDataError("bad signature") from e
    if float(jose.get("exp", 0)) + leeway < (time.time() if now is None else now):
        raise OidcDataError("token expired")
    try:
        return json.loads(_b64(p))
    except Exception as e:  # noqa: BLE001
        raise OidcDataError("malformed JWT payload") from e
