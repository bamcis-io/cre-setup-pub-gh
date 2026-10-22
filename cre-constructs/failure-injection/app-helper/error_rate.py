"""Reusable error-rate knob reader (5s TTL cache).

The parameter name is read from the ``ERROR_RATE_PARAM`` environment variable that the
``FailureInjectionParameter`` construct injects.

Wire (in the demo's CDK stack)::

    fn.addEnvironment('ERROR_RATE_PARAM', knob.parameterName)

Then in the demo's handler::

    from error_rate import should_fail  # vendored helper
    def handler(event, context):
        if should_fail():
            return {"statusCode": 500, "body": '{"error":"Internal Server Error"}'}
        ...  # normal path

Gotcha: after injecting failure, wait ~5s before
expecting full effect — that's the TTL.
"""
import os
import random
import time

import boto3

_PARAM_NAME = os.environ.get("ERROR_RATE_PARAM", "")
_CACHE_TTL = 5  # seconds — bounds GetParameter call rate; ~5s propagation delay
_ssm_client = None
_cached_error_rate = 0.0
_cache_expiry = 0.0


def get_error_rate() -> float:
    """Return the configured error rate (0-100) from SSM, cached with TTL.

    Returns 0 (no failure) if ``ERROR_RATE_PARAM`` is unset or the knob has never been
    read successfully, so the app stays GREEN until the knob is both deployed and consumed.

    On a failed read — SSM throttling under heavy fan-out, a transient network error, or a
    non-numeric value — it keeps the LAST KNOWN rate instead of resetting to 0. Resetting
    made an injected failure flicker off mid-demo whenever GetParameter was throttled.
    Accepts decimals ("12.5"); values are clamped to 0-100.
    """
    global _ssm_client, _cached_error_rate, _cache_expiry
    if not _PARAM_NAME:
        return 0
    now = time.monotonic()
    if now < _cache_expiry:
        return _cached_error_rate
    try:
        if _ssm_client is None:
            _ssm_client = boto3.client("ssm")
        resp = _ssm_client.get_parameter(Name=_PARAM_NAME)
        _cached_error_rate = min(100.0, max(0.0, float(resp["Parameter"]["Value"])))
    except Exception as exc:  # noqa: BLE001 — never let the knob break the app
        print(f"error_rate: keeping last value {_cached_error_rate} ({_PARAM_NAME}: {exc!r})")
    _cache_expiry = now + _CACHE_TTL
    return _cached_error_rate


def should_fail() -> bool:
    """Convenience: True for ~error_rate% of calls."""
    return random.random() * 100 < get_error_rate()
