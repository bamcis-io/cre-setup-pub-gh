"""POST/GET /inject — control-plane Lambda for the error-rate SSM knob.

The targets come from the ``TARGET_MAP`` env var (JSON) that the ``InjectApi`` construct
injects, so the Lambda is demo-independent.

- ``GET /inject``  -> current error rates for every target (logical -> int).
- ``POST /inject`` with body ``{"target": "<name>", "rate": <0-100>}`` -> set the rate.
"""
import json
import os

import boto3

_ssm = None
TARGET_MAP = json.loads(os.environ.get("TARGET_MAP", "{}"))


def handler(event, context):
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")

    method = event.get("httpMethod", "POST")

    if method == "GET":
        rates = {}
        for target, param in TARGET_MAP.items():
            try:
                # float, not int: the knob may hold a decimal set directly with
                # put-parameter (the reader honours "12.5"); int("12.5") raised and
                # reported 0.
                value = float(_ssm.get_parameter(Name=param)["Parameter"]["Value"])
                rates[target] = int(value) if value.is_integer() else value
            except Exception:
                rates[target] = 0
        return _resp(200, rates)

    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _resp(400, {"error": "Invalid JSON body"})

    target, rate = body.get("target"), body.get("rate")
    if target not in TARGET_MAP:
        return _resp(400, {"error": f"Invalid target. One of: {list(TARGET_MAP)}"})
    # bool is a subclass of int: without the explicit check `"rate": true` passed, was
    # written as "True", and the knob reader then parsed it as 0.
    if isinstance(rate, bool) or not isinstance(rate, int) or not 0 <= rate <= 100:
        return _resp(400, {"error": "rate must be an integer 0-100"})

    try:
        _ssm.put_parameter(Name=TARGET_MAP[target], Value=str(rate), Type="String", Overwrite=True)
    except Exception as e:
        return _resp(500, {"error": f"SSM put_parameter failed: {str(e)}"})

    return _resp(200, {"status": "ok", "target": target, "rate": rate, "parameter": TARGET_MAP[target]})


def _resp(code, body):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
