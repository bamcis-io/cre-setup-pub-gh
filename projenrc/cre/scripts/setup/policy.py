#!/usr/bin/env python3
"""IAM / S3 policy documents for the CRE setup scripts.

Every subcommand prints a JSON document on stdout. The ``merge-*`` subcommands take the current
document (``--current FILE``, ``-`` for stdin, or omitted for "none yet") and print
``{"changed": bool, "policy": {...}}`` so the caller can skip no-op updates and show a diff.

Pure functions, no AWS calls: covered by ``test/setup/test_policy.py``.
"""
import argparse
import copy
import difflib
import json
import os
import sys

VENDOR_JUMP_ROLE = "arn:aws:iam::979517299116:role/gitlab-runners-prod"
GITHUB_OIDC_HOST = "token.actions.githubusercontent.com"


def _p():
    """The partition every ARN here is built in (aws, aws-us-gov, aws-eusc, ...): setup/lib.sh
    exports CRE_PARTITION from the caller's own ARN, so GovCloud or EUSC accounts get their ARNs."""
    return os.environ.get("CRE_PARTITION", "aws")


def _load(path):
    if path is None:
        return None
    text = sys.stdin.read() if path == "-" else open(path).read()
    text = text.strip()
    return json.loads(text) if text else None


def _as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def _set_list(holder, key, values):
    """Store a list, collapsing a single value to a string as IAM does."""
    holder[key] = values[0] if len(values) == 1 else values


# ---- GitLab runner trust ---------------------------------------------------------------------

# Only jobs on a protected branch (main: push = No one, so only merged MRs) get the runner role.
# The role ARN isn't secret, and any branch's CI file can set AWS_CREDS_TARGET_ROLE itself, so
# protecting the CI/CD variable alone doesn't stop an unmerged branch from assuming the role (and
# passing the admin CloudFormation role). The vendor tags every jump with this key.
PROTECTED_BRANCH_KEY = "aws:PrincipalTag/GitLab:Project:ProtectedBranch"


def gitlab_statement(namespace, projects):
    stmt = {
        "Sid": "GitLabRunner" + "".join(ch for ch in namespace.title() if ch.isalnum())[:60],
        "Effect": "Allow",
        "Principal": {"AWS": VENDOR_JUMP_ROLE},
        "Action": ["sts:AssumeRole", "sts:TagSession"],
        "Condition": {"StringEquals": {"aws:PrincipalTag/GitLab:Group": namespace, PROTECTED_BRANCH_KEY: "Yes"}},
    }
    _set_list(stmt["Condition"]["StringEquals"], "aws:PrincipalTag/GitLab:Project", list(projects))
    return stmt


def _is_runner_stmt(s):
    principals = _as_list((s.get("Principal") or {}).get("AWS"))
    return s.get("Effect") == "Allow" and VENDOR_JUMP_ROLE in principals


def _group_stmts(stmts, namespace):
    """(guarded, unguarded) runner statements for `namespace`."""
    guarded, unguarded = [], []
    for s in stmts:
        if not _is_runner_stmt(s):
            continue
        eq = (s.get("Condition") or {}).get("StringEquals") or {}
        if eq.get("aws:PrincipalTag/GitLab:Group") != namespace:
            continue
        (guarded if eq.get(PROTECTED_BRANCH_KEY) == "Yes" else unguarded).append(s)
    return guarded, unguarded


def _projects(s):
    return _as_list(((s.get("Condition") or {}).get("StringEquals") or {}).get("aws:PrincipalTag/GitLab:Project"))


def merge_gitlab_trust(current, namespace, project):
    """Trust `project` for protected-branch jobs only: add it to the namespace's guarded statement,
    or append one. A statement without the protected-branch condition (written by hand, or before
    setup required it) is never changed: it trusts other projects, whose pipelines the condition
    could break. `unguarded_projects` reports what such statements still trust."""
    if current is None:
        return True, {"Version": "2012-10-17", "Statement": [gitlab_statement(namespace, [project])]}
    doc = copy.deepcopy(current)
    stmts = _as_list(doc.get("Statement"))
    doc["Statement"] = stmts
    guarded, _ = _group_stmts(stmts, namespace)
    for s in guarded:
        if project in _projects(s):
            return False, doc
    if guarded:
        eq = guarded[0]["Condition"]["StringEquals"]
        _set_list(eq, "aws:PrincipalTag/GitLab:Project", _projects(guarded[0]) + [project])
        return True, doc
    new = gitlab_statement(namespace, [project])
    if new["Sid"] in {st.get("Sid") for st in stmts}:   # an unguarded statement has the default Sid
        new["Sid"] = (new["Sid"] + "Protected")[:100]
    stmts.append(new)
    return True, doc


def unguarded_projects(current, namespace):
    """Projects of `namespace` a statement trusts without the protected-branch condition (any
    branch's job may assume the role for them)."""
    if current is None:
        return []
    _, unguarded = _group_stmts(_as_list(current.get("Statement")), namespace)
    return [p for s in unguarded for p in _projects(s)]


# ---- GitHub OIDC trust -----------------------------------------------------------------------

def github_subs(prefix):
    # The one subject the deploy workflow presents: a run on main (the merged PR, or a manual
    # dispatch from main). `prefix` is the repo's OIDC subject prefix as GitHub reports it:
    # `repo:<owner>/<repo>`, or with immutable subjects `repo:<owner>@<id>/<repo>@<id>`, which no
    # later repo of the same name can present.
    return [f"{prefix}:ref:refs/heads/main"]


def _repo_subs(subs, repo, prefix):
    """The subjects in `subs` that belong to this repo, under its name or its prefix."""
    return [x for x in subs if x.startswith(f"repo:{repo}:") or x.startswith(f"{prefix}:")]


def merge_github_trust(current, provider, repo, prefix=None):
    """`provider` is the OIDC provider ARN as IAM reports it. This repo's subjects become exactly
    github_subs(prefix): a stale one (an old `pull_request` subject, the name-only form of an
    immutable-subject repo) is dropped; other repos' subjects are kept."""
    prefix = prefix or f"repo:{repo}"
    wanted = github_subs(prefix)
    if current is None:
        return True, {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "GitHubActionsDeploy",
                "Effect": "Allow",
                "Principal": {"Federated": provider},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {"StringEquals": {
                    f"{GITHUB_OIDC_HOST}:aud": "sts.amazonaws.com",
                    f"{GITHUB_OIDC_HOST}:sub": wanted,
                }},
            }],
        }
    doc = copy.deepcopy(current)
    stmts = _as_list(doc.get("Statement"))
    doc["Statement"] = stmts
    for s in stmts:
        if (s.get("Principal") or {}).get("Federated") != provider:
            continue
        eq = (s.get("Condition") or {}).setdefault("StringEquals", {})
        subs = _as_list(eq.get(f"{GITHUB_OIDC_HOST}:sub"))
        mine = _repo_subs(subs, repo, prefix)
        if sorted(mine) == sorted(wanted):
            return False, doc
        _set_list(eq, f"{GITHUB_OIDC_HOST}:sub", [x for x in subs if x not in mine] + wanted)
        return True, doc
    _, fresh = merge_github_trust(None, provider, repo, prefix)
    stmts.append(fresh["Statement"][0])
    return True, doc


# ---- roles' permissions ----------------------------------------------------------------------

def cfn_trust():
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "cloudformation.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }]}


def runner_policy(account, exec_role_arn):
    """The runner role we create: shared by every CRE project in the account, so nothing in it is
    per project. Everything a stack creates is CloudFormation's (the execution role)."""
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "Identity", "Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
        {"Sid": "Stacks", "Effect": "Allow", "Action": [
            "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
            "cloudformation:DescribeStackResources", "cloudformation:ListStacks",
            "cloudformation:ValidateTemplate", "cloudformation:GetTemplateSummary",
            "cloudformation:GetTemplate", "cloudformation:CreateChangeSet",
            "cloudformation:DescribeChangeSet", "cloudformation:ExecuteChangeSet",
            "cloudformation:DeleteChangeSet", "cloudformation:ListChangeSets",
            "cloudformation:DeleteStack"],
         "Resource": f"arn:{_p()}:cloudformation:*:{account}:stack/*"},
        {"Sid": "AssetsBuckets", "Effect": "Allow", "Action": [
            "s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject", "s3:PutObject",
            "s3:DeleteObject"],
         "Resource": [f"arn:{_p()}:s3:::*", f"arn:{_p()}:s3:::*/*"],
         "Condition": {"StringEquals": {"aws:ResourceAccount": account}}},
        {"Sid": "EcrAuth", "Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
        {"Sid": "EcrPush", "Effect": "Allow", "Action": [
            "ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:DescribeImages",
            "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
            "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer"],
         "Resource": f"arn:{_p()}:ecr:*:{account}:repository/*"},
        {"Sid": "PassExecutionRole", "Effect": "Allow", "Action": "iam:PassRole",
         "Resource": exec_role_arn,
         "Condition": {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}},
        # External DNS in another account (setup/dns.sh): that account's role decides who may
        # assume it, so allowing the fixed name in any account grants nothing by itself.
        {"Sid": "AssumeDnsRole", "Effect": "Allow", "Action": ["sts:AssumeRole", "sts:TagSession"],
         "Resource": f"arn:{_p()}:iam::*:role/CreDnsDeploy"},
    ]}


def assets_bucket_policy(current, bucket, project, account):
    """Let another account (external DNS) read this project's templates."""
    sid = f"CreReadFrom{account}"
    stmt = {"Sid": sid, "Effect": "Allow", "Principal": {"AWS": f"arn:{_p()}:iam::{account}:root"},
            "Action": "s3:GetObject", "Resource": f"arn:{_p()}:s3:::{bucket}/{project}/*"}
    doc = copy.deepcopy(current) if current else {"Version": "2012-10-17", "Statement": []}
    stmts = _as_list(doc.get("Statement"))
    doc["Statement"] = stmts
    for s in stmts:
        if s.get("Sid") == sid:
            if s == stmt:
                return False, doc
            stmts[stmts.index(s)] = stmt
            return True, doc
    stmts.append(stmt)
    return True, doc


# ---- external DNS ----------------------------------------------------------------------------

def validator_role_arns(app_account, project, regions):
    """ARNs of the ValidatedCertificate validator roles (`<project>-acm-<region>`, no path).

    These are the one place an ARN is built from a name: the roles don't exist yet when the DNS
    role's trust is written (CloudFormation creates them later), so there is nothing to look up.
    The construct gives them exactly this name (constructs/src/dns/validated-certificate.ts)."""
    return [f"arn:{_p()}:iam::{app_account}:role/{project}-acm-{r}" for r in regions]


def merge_dns_trust(current, app_account, runner_role_arn, project, regions):
    """The zone account's DNS role: assumable by the app account's deploy role (CI, local) and by
    the project's certificate-validator roles, by exact ARN. A `<project>-*` wildcard written by
    an older setup is removed: it let every role the project's stacks create in."""
    sid = f"CreDnsFrom{app_account}"
    stale = f"arn:{_p()}:iam::{app_account}:role/{project}-*"
    principals = sorted({runner_role_arn, *validator_role_arns(app_account, project, regions)})
    want = {"Sid": sid, "Effect": "Allow", "Principal": {"AWS": f"arn:{_p()}:iam::{app_account}:root"},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Condition": {"ArnLike": {"aws:PrincipalArn": principals}}}
    if current is None:
        return True, {"Version": "2012-10-17", "Statement": [want]}
    doc = copy.deepcopy(current)
    stmts = _as_list(doc.get("Statement"))
    doc["Statement"] = stmts
    for s in stmts:
        if s.get("Sid") != sid:
            continue
        have = _as_list((s.get("Condition") or {}).get("ArnLike", {}).get("aws:PrincipalArn"))
        merged = sorted((set(have) - {stale}) | set(principals))
        if merged == sorted(have):
            return False, doc
        s.setdefault("Condition", {}).setdefault("ArnLike", {})["aws:PrincipalArn"] = merged
        return True, doc
    stmts.append(want)
    return True, doc


def dns_role_policy(account, zone_id, project, exec_role_arn, app_buckets):
    """The DNS role: deploy the <project>-dns stack (CI) and write validation records (Lambda).

    The dns stack's template sits in the app account's assets bucket, so reading it cross-account
    needs this identity grant AND the bucket-policy grant (merge-bucket-policy)."""
    templates = [f"arn:{_p()}:s3:::{b}/{project}/*" for b in app_buckets]
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "AppTemplates", "Effect": "Allow", "Action": "s3:GetObject", "Resource": templates},
        {"Sid": "Identity", "Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
        {"Sid": "StacksRead", "Effect": "Allow", "Resource": "*", "Action": [
            "cloudformation:DescribeStacks", "cloudformation:DescribeStackEvents",
            "cloudformation:ValidateTemplate", "cloudformation:GetTemplateSummary"]},
        {"Sid": "DnsStack", "Effect": "Allow", "Action": [
            "cloudformation:CreateChangeSet", "cloudformation:DescribeChangeSet",
            "cloudformation:ExecuteChangeSet", "cloudformation:DeleteChangeSet",
            "cloudformation:DeleteStack", "cloudformation:GetTemplate"],
         "Resource": f"arn:{_p()}:cloudformation:*:{account}:stack/{project}-dns/*"},
        {"Sid": "Records", "Effect": "Allow",
         "Action": ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets",
                    "route53:GetHostedZone"],
         "Resource": f"arn:{_p()}:route53:::hostedzone/{zone_id}"},
        {"Sid": "ChangeStatus", "Effect": "Allow", "Action": "route53:GetChange",
         "Resource": f"arn:{_p()}:route53:::change/*"},
        {"Sid": "PassExecutionRole", "Effect": "Allow", "Action": "iam:PassRole",
         "Resource": exec_role_arn,
         "Condition": {"StringEquals": {"iam:PassedToService": "cloudformation.amazonaws.com"}}},
    ]}


def dns_exec_policy(zone_id, project, app_buckets):
    """CloudFormation's role in the zone account: records in this one zone only."""
    return {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:GetObject",
         "Resource": [f"arn:{_p()}:s3:::{b}/{project}/*" for b in app_buckets]},
        {"Effect": "Allow",
         "Action": ["route53:ChangeResourceRecordSets", "route53:ListResourceRecordSets",
                    "route53:GetHostedZone"],
         "Resource": f"arn:{_p()}:route53:::hostedzone/{zone_id}"},
        {"Effect": "Allow", "Action": ["route53:GetChange", "route53:ListHostedZones"],
         "Resource": "*"},
    ]}


def nova_trust():
    return {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "nova.aws.internal"}, "Action": "sts:AssumeRole"}]}


# ---- CLI -------------------------------------------------------------------------------------

def _print(doc):
    print(json.dumps(doc, indent=2, sort_keys=True))


def _print_merge(changed, doc):
    print(json.dumps({"changed": changed, "policy": doc}, sort_keys=True))


def diff(a, b):
    pa = json.dumps(a, indent=2, sort_keys=True).splitlines() if a else []
    pb = json.dumps(b, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(pa, pb, "current", "proposed", lineterm=""))


def main(argv=None):
    p = argparse.ArgumentParser(prog="policy.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, *args):
        sp = sub.add_parser(name)
        for a in args:
            sp.add_argument(f"--{a}", required=a != "current", default=None)
        return sp

    add("merge-gitlab-trust", "current", "namespace", "project")
    add("merge-github-trust", "current", "provider-arn", "repo", "sub-prefix")
    add("merge-dns-trust", "current", "app-account", "runner-role-arn", "project", "regions")
    add("merge-bucket-policy", "current", "bucket", "project", "account")
    add("cfn-trust")
    add("nova-trust")
    add("runner-policy", "account", "exec-role-arn")
    add("dns-role-policy", "account", "zone-id", "project", "exec-role-arn", "app-buckets")
    add("dns-exec-policy", "zone-id", "project", "app-buckets")
    sp = sub.add_parser("diff")
    sp.add_argument("--current")
    sp.add_argument("--proposed", required=True)
    sp = sub.add_parser("get")
    sp.add_argument("key")
    a = p.parse_args(argv)
    cur = lambda: _load(getattr(a, "current", None))  # noqa: E731

    if a.cmd == "merge-gitlab-trust":
        _print_merge(*merge_gitlab_trust(cur(), a.namespace, a.project))
    elif a.cmd == "merge-github-trust":
        _print_merge(*merge_github_trust(cur(), a.provider_arn, a.repo, a.sub_prefix))
    elif a.cmd == "merge-dns-trust":
        _print_merge(*merge_dns_trust(cur(), a.app_account, a.runner_role_arn, a.project, a.regions.split()))
    elif a.cmd == "merge-bucket-policy":
        _print_merge(*assets_bucket_policy(cur(), a.bucket, a.project, a.account))
    elif a.cmd == "cfn-trust":
        _print(cfn_trust())
    elif a.cmd == "nova-trust":
        _print(nova_trust())
    elif a.cmd == "runner-policy":
        _print(runner_policy(a.account, a.exec_role_arn))
    elif a.cmd == "dns-role-policy":
        _print(dns_role_policy(a.account, a.zone_id, a.project, a.exec_role_arn, a.app_buckets.split()))
    elif a.cmd == "dns-exec-policy":
        _print(dns_exec_policy(a.zone_id, a.project, a.app_buckets.split()))
    elif a.cmd == "diff":
        print(diff(_load(a.current), _load(a.proposed)))
    elif a.cmd == "get":
        # Read {"changed":..,"policy":..} from stdin; print one field as JSON / plain text.
        doc = json.loads(sys.stdin.read())
        v = doc[a.key]
        print(json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else str(v).lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
