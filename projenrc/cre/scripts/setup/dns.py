#!/usr/bin/env python3
"""External DNS for a CRE project: a Route 53 zone the demo writes records (and ACM validation
records) into, in the deploy account or another account.

  dns.py --profile <app-profile> --project <name> --regions "<r1> ..." [--domain <fqdn>]
         (--zone-id <Z...> | --supernova people --alias <alias> | --supernova cre --zone-name <name>)
         [--dns-profile <zone-account-profile>] [--runner-role <name>] [--provider gitlab|github]
         [--bindle <id>] [--yes] [--dry-run]

The runner role defaults to the provider's (GitLabRunner on GitLab, CreGitHubDeploy on GitHub;
--provider, default gitlab). In a project (npx projen setup:dns) --project, --regions, --provider,
--runner-role, --zone-id and --domain default to cre-setup.json.

Zone in the deploy account (no --dns-profile, or one for the same account): checks the zone and
that the domain is inside it (default <project>.<zone>). Prints DOMAIN and HOSTED_ZONE_ID.

Zone in another account (--dns-profile for it):
  - zone account: role `CreDnsDeploy`, assumable only by the deploy account's runner role and this
    project's certificate-validator roles (`<project>-acm-<region>`, by exact ARN), allowed to deploy
    `<project>-dns` and write records in this one zone; and `CreDnsCfnExecution`, the CloudFormation
    role there, limited to the zone. One inline policy per project on each.
  - deploy account: the assets buckets let the zone account read this project's templates. The
    runner role is not changed: it is checked (read-only) for sts:AssumeRole on CreDnsDeploy, which
    a runner role created by aws_account.py already has.
  Prints DOMAIN, HOSTED_ZONE_ID, DNS_ROLE_ARN, DNS_CFN_EXECUTION_ROLE_ARN.

No zone yet (--supernova): creates the public hosted zone and the `Nova-DO-NOT-DELETE` role
SuperNova needs (trusted by nova.aws.internal; AmazonRoute53FullAccess + SecurityAudit), then
prints what to submit at https://supernova.amazon.dev, with DELEGATION=pending and NEEDS=DELEGATION
(exit 13). Re-run with --zone-id once delegation completes.

Never deletes a hosted zone: deleting a delegated zone by hand leaves the domain open to sniping.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402
import policy  # noqa: E402

NAME = "dns"
DNS_ROLE, DNS_EXEC, NOVA_ROLE = "CreDnsDeploy", "CreDnsCfnExecution", "Nova-DO-NOT-DELETE"


def parse(step, argv):
    p = c.Parser(step, "dns.py")
    for flag in ("profile", "dns-profile", "project", "regions", "domain", "zone-id", "supernova", "alias",
                 "zone-name", "runner-role", "provider"):
        p.add_argument(f"--{flag}", dest=flag.replace("-", "_"), default="")
    p.add_argument("--bindle", default=os.environ.get("BINDLE_ID", "amzn1.bindle.resource.v4o66fxvd3zfcqvcmhxq"))
    c.common_flags(p)
    a = p.parse_args(argv)
    step.yes, step.dry_run = a.yes, a.dry_run
    sf = c.SetupFile(step)
    for attr, key in (("project", "project"), ("regions", "regions"), ("provider", "provider"),
                      ("runner_role", "runnerRole")):
        sf.default(a, attr, key)
    if not a.supernova:
        sf.default(a, "zone_id", "hostedZoneId")
        sf.default(a, "domain", "domain")
    sf.report()
    if isinstance(a.regions, list):
        a.regions = " ".join(a.regions)
    a.regions = a.regions.split()
    a.domain = a.domain.rstrip(".")
    a.zone_name = a.zone_name.rstrip(".")
    a.zone_id = a.zone_id.replace("/hostedzone/", "")
    provider = a.provider or "gitlab"
    if provider == "gitlab":
        a.runner_role = a.runner_role or "GitLabRunner"
    elif provider == "github":
        a.runner_role = a.runner_role or "CreGitHubDeploy"
    else:
        step.die(c.REFUSED, "--provider must be gitlab or github.")
    if not a.project or not a.regions:
        step.die(c.REFUSED, "--project and --regions are required.")
    if not a.zone_id and not a.supernova:
        step.die(c.REFUSED, "Give --zone-id, or --supernova people|cre to set up a new zone.")
    return a


def supernova(step, zone_aws, a):
    if a.supernova == "people":
        if not a.alias:
            step.die(c.REFUSED, "--supernova people needs --alias <your alias>.")
        zone_name, org = f"{a.alias}.people.aws.dev", "People"
    elif a.supernova == "cre":
        if not a.zone_name:
            step.die(c.REFUSED, "--supernova cre needs --zone-name <sub>.<cre org domain> (check the org's domain on supernova.amazon.dev).")
        zone_name, org = a.zone_name, "cre"
    else:
        step.die(c.REFUSED, "--supernova must be people or cre.")
    existing = zone_aws.text("route53", "list-hosted-zones-by-name", "--dns-name", f"{zone_name}.", "--max-items", "1",
                             "--query", f"HostedZones[?Name=='{zone_name}.' && Config.PrivateZone==`false`].Id | [0]",
                             "--output", "text")
    if existing and existing != "None":
        zone_id = existing.replace("/hostedzone/", "")
        step.ok(f"Public hosted zone {zone_name} exists ({zone_id}).")
    else:
        res = step.apply(f"create public hosted zone {zone_name}", zone_aws.call, "route53", "create-hosted-zone",
                         "--name", zone_name, "--caller-reference", f"cre-{a.project}-{int(time.time())}",
                         "--hosted-zone-config", f"Comment=CRE {a.project} (SuperNova delegated)",
                         "--query", "HostedZone.Id", "--output", "text")
        if res is not None and res.returncode != 0:
            step.die(c.FAILED, f"create-hosted-zone {zone_name} failed: {c.last_line(res.stderr)}")
        zone_id = res.stdout.strip().replace("/hostedzone/", "") if res is not None else "<created on a real run>"
    nova_arn = zone_aws.role_arn(NOVA_ROLE)
    if nova_arn:
        step.ok(f"Role {NOVA_ROLE} exists.")
    else:
        tmp = c.tmpdir(step)
        doc = os.path.join(tmp, "nova.json")
        c.write_json(doc, policy.nova_trust())
        res = step.apply(f"create role {NOVA_ROLE} (trusted by nova.aws.internal)", zone_aws.call, "iam", "create-role",
                         "--role-name", NOVA_ROLE, "--assume-role-policy-document", f"file://{doc}",
                         "--description", "SuperNova Route53 Access Role", "--query", "Role.Arn", "--output", "text")
        if res is not None:
            if res.returncode != 0:
                step.die(c.FAILED, f"create-role {NOVA_ROLE} failed: {c.last_line(res.stderr)}")
            nova_arn = res.stdout.strip()
        for pol in ("AmazonRoute53FullAccess", "SecurityAudit"):
            res = step.apply(f"attach {pol} to {NOVA_ROLE}", zone_aws.call, "iam", "attach-role-policy", "--role-name", NOVA_ROLE,
                             "--policy-arn", f"arn:{zone_aws.partition}:iam::aws:policy/{pol}")
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"attach {pol} failed: {c.last_line(res.stderr)}")
    print(f"""
Submit the delegation at https://supernova.amazon.dev → New Sub-Domain Request:
  Company / Organization : AWS / {org}
  Domain                 : {zone_name}
  Bindle                 : {a.bindle}
  IAM role ARN for Nova  : {nova_arn or '<created on a real run>'}
  Hosted zone ID         : {zone_id}      (fill it in: left blank, SuperNova creates a second zone)
Then add the anti-spoofing TXT records from SuperNova's post-onboarding page, and re-run with
  --zone-id {zone_id}
Never delete this zone by hand; retire the domain through SuperNova.""", file=sys.stderr)
    step.out("HOSTED_ZONE_ID", zone_id)
    step.out("DELEGATION", "pending")
    step.need("DELEGATION")


def put_dns_policy(step, zone_aws, tmp, role, doc, project, note=""):
    """The per-project inline policy on a zone-account role this script owns; skipped when IAM
    already holds the same document, so a re-run changes nothing."""
    name = f"cre-dns-{project}"
    current = zone_aws.json("iam", "get-role-policy", "--role-name", role, "--policy-name", name,
                            "--query", "PolicyDocument", "--output", "json")
    if current is not None and not policy.diff(current, doc):
        step.ok(f"{role} already has the {name} policy.")
        return
    f = os.path.join(tmp, f"{role}.json")
    c.write_json(f, doc)
    res = step.apply(f"put inline policy {name} on {role}{(' ' + note) if note else ''}", zone_aws.call, "iam", "put-role-policy",
                     "--role-name", role, "--policy-name", name, "--policy-document", f"file://{f}")
    if res is not None and res.returncode != 0:
        step.die(c.FAILED, f"put-role-policy on {role} failed: {c.last_line(res.stderr)}")


def cross_account(step, app_aws, zone_aws, a):
    tmp = c.tmpdir(step)
    buckets = [f"{a.project}-{r}" for r in a.regions]
    # Every ARN below is IAM's answer (get-role, or create-role's output), never built from a name.
    runner_arn = app_aws.role_arn(a.runner_role)
    if not runner_arn:
        step.die(c.REFUSED, f"Runner role {a.runner_role} doesn't exist in {app_aws.account}; run aws_account.py first, or pass --runner-role.")

    # CloudFormation's role in the zone account.
    exec_arn = zone_aws.role_arn(DNS_EXEC)
    if not exec_arn:
        doc = os.path.join(tmp, "cfn.json")
        c.write_json(doc, policy.cfn_trust())
        res = step.apply(f"create role {DNS_EXEC} in {zone_aws.account}", zone_aws.call, "iam", "create-role", "--role-name", DNS_EXEC,
                         "--assume-role-policy-document", f"file://{doc}", "--description", "CloudFormation role for CRE DNS stacks",
                         "--query", "Role.Arn", "--output", "text")
        if res is not None:
            if res.returncode != 0:
                step.die(c.FAILED, f"create-role {DNS_EXEC} failed: {c.last_line(res.stderr)}")
            exec_arn = res.stdout.strip()
    put_dns_policy(step, zone_aws, tmp, DNS_EXEC, policy.dns_exec_policy(a.zone_id, a.project, buckets), a.project,
                   f"(zone {a.zone_id} only)")

    # The role CI and the certificate Lambda assume.
    role_arn = zone_aws.role_arn(DNS_ROLE)
    current = None
    if role_arn:
        current = zone_aws.json("iam", "get-role", "--role-name", DNS_ROLE, "--query", "Role.AssumeRolePolicyDocument", "--output", "json")
        if current is None:
            step.die(c.FAILED, f"get-role {DNS_ROLE} failed.")
    changed, proposed = policy.merge_dns_trust(current, app_aws.account, runner_arn, a.project, a.regions)
    trust_file = os.path.join(tmp, "dns-trust.json")
    c.write_json(trust_file, proposed)
    if current is None:
        res = step.apply(f"create role {DNS_ROLE} in {zone_aws.account}", zone_aws.call, "iam", "create-role", "--role-name", DNS_ROLE,
                         "--assume-role-policy-document", f"file://{trust_file}", "--description", "CRE external DNS deploy role",
                         "--query", "Role.Arn", "--output", "text")
        if res is not None:
            if res.returncode != 0:
                step.die(c.FAILED, f"create-role {DNS_ROLE} failed: {c.last_line(res.stderr)}")
            role_arn = res.stdout.strip()
    elif changed:
        step.info(f"Proposed trust policy change for {DNS_ROLE}:")
        print(policy.diff(current, proposed), file=sys.stderr)
        if not step.confirm(f"Update the trust policy of {DNS_ROLE}?"):
            if not step.dry_run:
                step.die(c.REFUSED, f"Not updating {DNS_ROLE}; the deploy can't assume it.")
        else:
            res = step.apply(f"update trust policy of {DNS_ROLE}", zone_aws.call, "iam", "update-assume-role-policy",
                             "--role-name", DNS_ROLE, "--policy-document", f"file://{trust_file}")
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"update-assume-role-policy on {DNS_ROLE} failed: {c.last_line(res.stderr)}")
    else:
        step.ok(f"{DNS_ROLE} already trusts this project.")
    put_dns_policy(step, zone_aws, tmp, DNS_ROLE,
                   policy.dns_role_policy(zone_aws.account, a.zone_id, a.project, exec_arn, buckets), a.project)

    # Deploy account: the runner must be allowed to assume the DNS role. A runner role setup created
    # has that in its cre-runner policy; a provided one is never changed, only checked.
    if role_arn and app_aws.can(runner_arn, "sts:AssumeRole", role_arn):
        step.ok(f"{a.runner_role} may assume {role_arn}.")
    elif role_arn:
        step.warn(f"{a.runner_role} isn't allowed to assume {role_arn}, so the DNS deploy phase will fail. Its owner needs to grant\n"
                  f"   sts:AssumeRole + sts:TagSession on {role_arn} (this role is not changed by setup).")
        step.out("RUNNER_PERMISSION_GAPS", f"sts:AssumeRole({role_arn})")
    for b in buckets:
        res = app_aws.call("s3api", "get-bucket-policy", "--bucket", b, "--query", "Policy", "--output", "text")
        if res.returncode != 0 and "NoSuchBucketPolicy" not in res.stderr:
            # Anything but "no policy" (access denied, throttling): never replace a policy we couldn't read.
            step.die(c.FAILED, f"Reading the bucket policy of {b} failed: {c.last_line(res.stderr)}")
        raw = res.stdout.strip() if res.returncode == 0 else ""
        try:
            cur = json.loads(raw) if raw and raw != "None" else None
        except ValueError:
            step.die(c.FAILED, f"The bucket policy of {b} isn't valid JSON: {raw[:200]}")
        changed, doc = policy.assets_bucket_policy(cur, b, a.project, zone_aws.account)
        if not changed:
            step.ok(f"s3://{b} already readable by {zone_aws.account}.")
            continue
        f = os.path.join(tmp, f"bp-{b}.json")
        c.write_json(f, doc)
        res = step.apply(f"let account {zone_aws.account} read s3://{b}/{a.project}/*", app_aws.call, "s3api", "put-bucket-policy",
                         "--bucket", b, "--policy", f"file://{f}")
        if res is not None and res.returncode != 0:
            step.die(c.FAILED, f"put-bucket-policy on {b} failed: {c.last_line(res.stderr)}")
    step.out("DNS_ROLE_ARN", role_arn)
    step.out("DNS_CFN_EXECUTION_ROLE_ARN", exec_arn)


def main(step, argv):
    a = parse(step, argv)
    app_aws = c.Aws(step, a.profile).require()
    zone_aws = app_aws
    if a.dns_profile:
        zone_aws = c.Aws(step, a.dns_profile).require()
    cross = zone_aws.account != app_aws.account

    if not a.zone_id:
        supernova(step, zone_aws, a)
        return

    r = zone_aws.call("route53", "get-hosted-zone", "--id", a.zone_id, "--query", "HostedZone.Name", "--output", "text")
    if r.returncode != 0:
        step.die(c.REFUSED, f"Hosted zone {a.zone_id} not found in account {zone_aws.account}: {c.last_line(r.stderr)}")
    zone = r.stdout.strip().rstrip(".")
    domain = a.domain or f"{a.project}.{zone}"
    if domain != zone and not domain.endswith(f".{zone}"):
        step.die(c.REFUSED, f"Domain {domain} is not inside zone {zone}.")
    step.ok(f"Zone {zone} ({a.zone_id}) in account {zone_aws.account}; domain {domain}.")
    step.out("DOMAIN", domain)
    step.out("HOSTED_ZONE_ID", a.zone_id)
    if not cross:
        step.info("Zone is in the deploy account: no extra roles needed.")
        return
    cross_account(step, app_aws, zone_aws, a)


if __name__ == "__main__":
    c.cli(NAME, main)
