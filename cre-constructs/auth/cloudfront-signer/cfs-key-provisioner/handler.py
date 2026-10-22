"""Custom resource: generates an RSA key pair, uploads the public key to CloudFront,
and writes the private key + public key ID into a CFN-managed Secrets Manager secret.

The private key never leaves this Lambda except into Secrets Manager.

Design
------
The signing secret is a NATIVE CloudFormation resource (see distribution-signer.ts):
created with removalPolicy DESTROY and encrypted with the stack's CMK. This custom
resource does NOT create or own the secret — it only *populates* it via
`put_secret_value`. Because the secret and the CMK now share one stack lifecycle
(created together, deleted together), a retained secret can never end up bound to a
CMK the stack no longer owns — the "Access to KMS is not allowed" drift class is
structurally impossible. The private key is still generated inside this Lambda and
written straight into the secret, so it never appears in the CloudFormation template.

Properties (from CDK):
  KmsKeyId:           KMS key ID used to generate the RSA data-key-pair.
  ResourceNameSuffix: Unique suffix for naming the CloudFront public key.
  SecretArn:          ARN of the CFN-managed secret to populate.
  KeyPairVersion:     Bump to force key-pair regeneration on an already-deployed stack.

Returns:
  PublicKeyId: CloudFront public key ID. Also the PhysicalResourceId, so that a
               regenerated pair's superseded public key is cleaned up (via a Delete on
               the old id) whenever a regeneration changes it.

Known limitations (not fixed; see constructs/README.md §6):
  - Every Create/Update mints a NEW pair, including the Update CloudFormation sends
    when rolling back a failed stack update. The rollback overwrites the secret with a
    third pair while the key group may be restored to an older key id, so viewers can
    get 403s until the next successful deploy. Redeploying fixes it.
  - The secret is rewritten before the key group picks up the new public key, so a
    rotation has a short window where cookies are signed with a key CloudFront does not
    trust yet.
"""

import base64
import json
import os
import traceback
import urllib.request
import uuid

import boto3
from botocore.exceptions import ClientError


def send_response(event, context, status, data=None, reason=None, physical_id=None):
    body = json.dumps({
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {context.log_stream_name}",
        # PhysicalResourceId is the CloudFront public key id so that CloudFormation
        # cleans up the *superseded* public key (via a Delete on the old id) whenever a
        # regeneration changes it. Falls back to the incoming id, then the log stream.
        "PhysicalResourceId": physical_id or event.get("PhysicalResourceId") or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    })
    req = urllib.request.Request(event["ResponseURL"], data=body.encode(), method="PUT")
    req.add_header("Content-Type", "")
    urllib.request.urlopen(req)


def handler(event, context):
    request_type = event["RequestType"]
    props = event.get("ResourceProperties", {})
    try:
        if request_type in ("Create", "Update"):
            data, physical_id = _provision(props)
            send_response(event, context, "SUCCESS", data, physical_id=physical_id)
        else:  # Delete
            _cleanup_public_key(event.get("PhysicalResourceId"))
            send_response(event, context, "SUCCESS")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        # A failed Delete must never wedge stack teardown — report success and move on.
        if request_type == "Delete":
            send_response(event, context, "SUCCESS", reason=f"cleanup best-effort: {e}")
        else:
            send_response(event, context, "FAILED", reason=str(e))


def _provision(props):
    region = os.environ.get("AWS_REGION", "us-east-1")
    kms_key_id = props["KmsKeyId"]
    suffix = props["ResourceNameSuffix"]
    secret_id = props["SecretArn"]

    kms_client = boto3.client("kms", region_name=region)
    sm_client = boto3.client("secretsmanager", region_name=region)
    cf_client = boto3.client("cloudfront")

    # A unique public-key name per generation avoids CloudFront's name-collision when a
    # stale key from a prior generation still exists during the transition.
    public_key_name = f"CFSPublicKey-{suffix}-{uuid.uuid4().hex[:8]}"

    # 1) Fresh RSA pair. The private key exists only in this Lambda + the secret.
    resp = kms_client.generate_data_key_pair(KeyId=kms_key_id, KeyPairSpec="RSA_2048")
    private_pem = _der_to_pem(resp["PrivateKeyPlaintext"], "RSA PRIVATE KEY")
    public_pem = _der_to_pem(resp["PublicKey"], "PUBLIC KEY")

    # 2) Upload the public key to CloudFront.
    account_id = boto3.client("sts").get_caller_identity()["Account"]
    create_resp = cf_client.create_public_key(PublicKeyConfig={
        "CallerReference": f"cfs-{suffix}-{account_id}-{uuid.uuid4().hex}",
        "Name": public_key_name,
        "EncodedKey": public_pem,
        "Comment": "CloudFrontSigner public key for cookie signing",
    })
    public_key_id = create_resp["PublicKey"]["Id"]

    # 3) Write the material into the CFN-managed secret. The secret is already encrypted
    #    with the stack's CMK, so we only put the value — CloudFormation owns the
    #    secret's lifecycle (create/delete), which is what makes the key/secret binding
    #    drift-free.
    #
    #    If this fails, the FAILED response carries no new physical id, so CloudFormation
    #    never sends a Delete for the key we just created — remove it here or it leaks
    #    against CloudFront's small per-account public-key quota.
    try:
        sm_client.put_secret_value(
            SecretId=secret_id,
            SecretString=json.dumps({"PUBLIC_KEY_ID": public_key_id, "PRIVATE_KEY": private_pem}),
        )
    except Exception:
        _cleanup_public_key(public_key_id)
        raise

    return ({"PublicKeyId": public_key_id}, public_key_id)


def _cleanup_public_key(public_key_id):
    """Best-effort delete of a CloudFront public key by id.

    Fires both on stack teardown and when a regenerated pair supersedes an old public
    key. The signing secret is a native CloudFormation resource now, so its deletion is
    handled by CloudFormation — never here.
    """
    # Legacy physical ids (log-stream names from the old handler) are not public keys.
    if not public_key_id or not public_key_id.startswith("K"):
        return
    cf_client = boto3.client("cloudfront")
    try:
        etag = cf_client.get_public_key(Id=public_key_id)["ETag"]
        cf_client.delete_public_key(Id=public_key_id, IfMatch=etag)
    except ClientError as e:
        print(f"public key {public_key_id} cleanup skipped: {e}")


def _der_to_pem(der_bytes, label):
    b64 = base64.b64encode(der_bytes).decode()
    lines = [b64[i:i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"
