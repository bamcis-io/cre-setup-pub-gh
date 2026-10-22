"""Custom resource: write an ACM certificate's DNS validation records into a hosted zone in ANOTHER
account, so a certificate validates with no manual step.

Runs in parallel with the certificate (no dependency between them): CloudFormation creates the
certificate and then waits for it to be issued inside the same stack, so the validation records
must be written while that wait is under way. This handler polls ACM for PENDING_VALIDATION
certificates for the domain, assumes the zone account's role, and UPSERTs each validation CNAME.

Same-account zones never reach this handler: the construct uses CloudFormation's native
DomainValidationOptions.HostedZoneId there (a CloudFormation Condition picks the path at deploy
time, since whether the zone is cross-account is known only then).

Delete is a no-op: a leftover validation CNAME is harmless, and removing one that another
certificate for the same name still needs would break that certificate's renewal.

Invoked through the CDK custom-resource Provider framework: return a dict, raise on failure.
"""
import os
import time

import boto3
from botocore.config import Config

# Module-scope clients, short timeouts, few retries: a hanging call must not outlive the Lambda.
_CFG = Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 2, "mode": "standard"})
ACM = boto3.client("acm", config=_CFG)
STS = boto3.client("sts", config=_CFG)

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10"))
DEADLINE_SECONDS = int(os.environ.get("DEADLINE_SECONDS", "720"))  # Lambda timeout is 14 min


def _pending_records(domain, names):
    """Validation ResourceRecords of every pending certificate whose names match exactly.

    Returns (records, complete): complete once a matching certificate exists and every one of its
    validation options carries a ResourceRecord (ACM fills them in a few seconds after creation).
    """
    records, found, complete = {}, False, True
    paginator = ACM.get_paginator("list_certificates")
    for page in paginator.paginate(CertificateStatuses=["PENDING_VALIDATION"]):
        for summary in page.get("CertificateSummaryList", []):
            if summary.get("DomainName") != domain:
                continue
            cert = ACM.describe_certificate(CertificateArn=summary["CertificateArn"])["Certificate"]
            if set(cert.get("SubjectAlternativeNames", [domain])) != names:
                continue
            found = True
            opts = cert.get("DomainValidationOptions", [])
            for opt in opts:
                rr = opt.get("ResourceRecord")
                if rr:
                    records[rr["Name"]] = rr["Value"]
                else:
                    complete = False
            if not opts:
                complete = False
    return records, found and complete


def _issued(domain, names):
    """True when an ISSUED certificate with exactly these names exists (nothing to validate)."""
    for page in ACM.get_paginator("list_certificates").paginate(CertificateStatuses=["ISSUED"]):
        for summary in page.get("CertificateSummaryList", []):
            if summary.get("DomainName") != domain:
                continue
            cert = ACM.describe_certificate(CertificateArn=summary["CertificateArn"])["Certificate"]
            if set(cert.get("SubjectAlternativeNames", [domain])) == names:
                return True
    return False


GRACE_SECONDS = int(os.environ.get("GRACE_SECONDS", "60"))


def _route53(role_arn):
    creds = STS.assume_role(RoleArn=role_arn, RoleSessionName="cre-cert-validation",
                            DurationSeconds=900)["Credentials"]
    return boto3.client("route53", config=_CFG, aws_access_key_id=creds["AccessKeyId"],
                        aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"])


def handler(event, _context, sleep=time.sleep, clock=time.monotonic):
    props = event["ResourceProperties"]
    domain = props["DomainName"]
    names = set([domain] + list(props.get("SubjectAlternativeNames") or []))
    physical = event.get("PhysicalResourceId") or f"{domain}-validation"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": physical}

    # The certificate is created concurrently; wait until it exists with all its records.
    start = clock()
    while True:
        records, complete = _pending_records(domain, names)
        if complete:
            break
        # An Update that changed only the zone/role leaves an already-issued certificate in place;
        # no new pending one will appear. Give a replacement certificate a grace period first.
        if not records and clock() - start > GRACE_SECONDS and _issued(domain, names):
            return {"PhysicalResourceId": physical, "Data": {"Records": "0"}}
        if clock() - start > DEADLINE_SECONDS:
            raise RuntimeError(f"No PENDING_VALIDATION certificate for {sorted(names)} appeared within "
                               f"{DEADLINE_SECONDS}s")
        sleep(POLL_SECONDS)

    r53 = _route53(props["DnsRoleArn"])
    changes = [{"Action": "UPSERT", "ResourceRecordSet": {
        "Name": name, "Type": "CNAME", "TTL": 300, "ResourceRecords": [{"Value": value}]}}
        for name, value in sorted(records.items())]
    r53.change_resource_record_sets(HostedZoneId=props["HostedZoneId"], ChangeBatch={
        "Comment": f"ACM validation for {domain}", "Changes": changes})
    return {"PhysicalResourceId": physical, "Data": {"Records": str(len(changes))}}
