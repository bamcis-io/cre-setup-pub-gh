"""Runtime-config endpoint for the Cognito + API Gateway (JWT) auth variant.

Returns the Cognito identifiers the static SPA bundle
needs to run the Amplify/Cognito login flow, so the same build works across deploys and
accounts WITHOUT baking per-deploy IDs into the bundle.

Wired at GET /amplify-config and left PUBLIC (no authorizer) — these are non-secret,
client-side discoverable identifiers (region, user pool id, app client id), the same values
the hosted UI exposes. The protected business routes use the JWT authorizer instead.
"""
import json
import os

_HEADERS = {
    "Content-Type": "application/json",
    # The SPA fetches this from the browser; allow it. Values are non-secret.
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
}


def handler(event, context):  # noqa: ARG001 - Lambda signature
    body = {
        "region": os.environ.get("REGION", ""),
        "userPoolId": os.environ.get("USER_POOL_ID", ""),
        "appClientId": os.environ.get("APP_CLIENT_ID", ""),
    }
    return {
        "statusCode": 200,
        "headers": _HEADERS,
        "body": json.dumps(body),
    }
