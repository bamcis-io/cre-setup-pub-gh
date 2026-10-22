#!/usr/bin/env python3
"""Prepare an AWS account to receive a CRE project's deploys.

  aws_account.py --profile <p> --project <name> --regions "<r1> [r2 ...]" --provider gitlab|github
                 (--namespace <gitlab/group/path> | --github-repo <owner>/<repo>)
                 [--runner-role <name>] [--exec-role <name>] [--yes] [--dry-run]

In a project (npx projen setup:aws), every flag but --profile defaults to cre-setup.json.

Does, idempotently:
  1. Checks the profile's credentials (exit 11 if missing/expired) and asks before a prod-looking account.
  2. One assets bucket per region, `<project>-<region>`, always including us-east-1 (the edge stack):
     private (Block Public Access on), bucket-owner-enforced, SSE-S3, tagged project=<name>. A name
     owned by another account stops (S3 names are global).
  3. The CloudFormation execution role (default `CreCfnExecution`), which every stack deploys with
     as --role-arn. An existing role is only verified: AdministratorAccess attached and
     cloudformation.amazonaws.com trusted (exit 10 otherwise; it is never changed). A missing one
     is created that way.
  4. The CI runner role (default `GitLabRunner` on GitLab, `CreGitHubDeploy` on GitHub), which
     uploads assets to S3, pushes images to ECR and passes the execution role to CloudFormation.
       - GitLab trust: the AWS Credential Vendor jump role, for this repo's group and project, and
         only for jobs on a protected branch (`GitLab:Project:ProtectedBranch = Yes`).
       - GitHub trust: the token.actions.githubusercontent.com OIDC provider (created if absent),
         for exactly this repo's `main` subject, in the form GitHub reports for the repo
         (immutable subjects for new repos), so the repo must exist first (repo_github.py prepare).
     An existing role gets only this project added to its trust (diff + confirm). Its permissions
     are never touched: they are checked read-only (iam simulate-principal-policy) and gaps are
     warned about. A missing role is created with that trust and one account-wide inline policy,
     `cre-runner`, which every project using the role shares.

Prints: ACCOUNT_ID, ASSETS_BUCKET_PREFIX, RUNNER_ROLE_ARN, CFN_EXECUTION_ROLE_ARN
(and RUNNER_PERMISSION_GAPS when an existing runner role may lack something).
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402
import policy  # noqa: E402

NAME = "aws-account"


def parse(step, argv):
    p = c.Parser(step, "aws_account.py")
    p.add_argument("--profile", default="")
    p.add_argument("--project", default="")
    p.add_argument("--regions", default="")
    p.add_argument("--provider", default="")
    p.add_argument("--namespace", default="")
    p.add_argument("--github-repo", dest="github_repo", default="")
    p.add_argument("--runner-role", dest="runner_role", default="")
    p.add_argument("--exec-role", dest="exec_role", default="")
    c.common_flags(p)
    a = p.parse_args(argv)
    step.yes, step.dry_run = a.yes, a.dry_run
    sf = c.SetupFile(step)
    for attr, key in (("project", "project"), ("regions", "regions"), ("provider", "provider"),
                      ("namespace", "namespace"), ("github_repo", "githubRepo"),
                      ("runner_role", "runnerRole"), ("exec_role", "execRole")):
        sf.default(a, attr, key)
    sf.report()
    if isinstance(a.regions, list):
        a.regions = " ".join(a.regions)
    a.regions = a.regions.split()
    # In a project: also every region a stack in cre-stacks.json deploys to, so a stack added in a
    # new region gets its bucket from a plain `npx projen setup:aws` (check:deploy-prereqs says so).
    plan_file = os.path.join(sf.dir, "cre-stacks.json") if sf.dir else ""
    if plan_file and os.path.isfile(plan_file):
        try:
            with open(plan_file) as f:
                planned = [s.get("region") for s in json.load(f).get("stacks", []) if s.get("region")]
        except (OSError, ValueError):
            planned = []
        extra = [r for r in dict.fromkeys(planned) if r not in a.regions and r != "us-east-1"]
        if extra:
            step.info(f"cre-stacks.json also deploys to {' '.join(extra)}; adding {'it' if len(extra) == 1 else 'them'}.")
            a.regions += extra
    a.setup_file = sf
    a.exec_role = a.exec_role or "CreCfnExecution"
    if not a.project:
        step.die(c.REFUSED, "--project is required.")
    if not a.regions:
        step.die(c.REFUSED, "--regions is required (space-separated).")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}[a-z0-9]", a.project):
        step.die(c.REFUSED, f"Project name '{a.project}' must be 3-42 lowercase letters, digits or hyphens (it prefixes S3 bucket names).")
    if a.provider == "gitlab":
        if not a.namespace:
            step.die(c.REFUSED, "--namespace is required for GitLab (e.g. aws-cre/resilience-demos).")
        a.runner_role = a.runner_role or "GitLabRunner"
    elif a.provider == "github":
        if not a.github_repo:
            step.die(c.REFUSED, "--github-repo <owner>/<repo> is required for GitHub.")
        a.runner_role = a.runner_role or "CreGitHubDeploy"
    else:
        step.die(c.REFUSED, "--provider must be gitlab or github.")
    return a


def github_sub_prefix(step, repo):
    """The repo's OIDC subject prefix, from GitHub: new repos use immutable subjects
    (`repo:<owner>@<id>/<repo>@<id>`), which can't be built from the name. So the repo must exist."""
    c.require_tools(step, "gh")
    c.require_gh(step)
    r = c.gh("api", f"repos/{repo}/actions/oidc/customization/sub")
    if r.returncode != 0:
        step.die(c.REFUSED, f"Could not read {repo}'s OIDC subject settings: {c.last_line(r.stderr or r.stdout)}. "
                 f"Create the repo first (repo_github.py prepare --repo {repo}), then re-run.")
    try:
        d = json.loads(r.stdout)
    except ValueError:
        step.die(c.FAILED, f"GitHub returned an unreadable OIDC subject setting for {repo}: {r.stdout[:200]}")
    if d.get("use_default") is False:
        step.die(c.REFUSED, f"{repo} uses a custom OIDC subject template; the deploy role trusts only the default subject. "
                 f"Reset it: gh api -X PUT repos/{repo}/actions/oidc/customization/sub -F use_default=true")
    prefix = d.get("sub_claim_prefix") or f"repo:{repo}"
    step.info(f"GitHub OIDC subject for {repo}: {prefix}:ref:refs/heads/main")
    return prefix


def ensure_buckets(step, aws, project, regions):
    # Plus us-east-1 whatever the project's regions: the edge stack (CloudFront + CloudFront Signer)
    # deploys there and needs its template and Lambda code in a bucket in that region.
    bucket_regions = list(regions)
    if "us-east-1" not in bucket_regions:
        bucket_regions.append("us-east-1")
        step.info("Also preparing a us-east-1 bucket: the edge stack (CloudFront Signer) deploys there.")
    for region in bucket_regions:
        b = f"{project}-{region}"
        r = aws.call("s3api", "head-bucket", "--bucket", b, "--expected-bucket-owner", aws.account)
        err = r.stderr + r.stdout
        if r.returncode == 0:
            step.ok(f"Bucket {b} exists in this account.")
        elif re.search(r"\b(404|Not Found|NoSuchBucket)\b", err):
            create = ["s3api", "create-bucket", "--bucket", b, "--region", region]
            if region != "us-east-1":
                create += ["--create-bucket-configuration", f"LocationConstraint={region}"]
            res = step.apply(f"create bucket {b} in {region}", aws.call, *create)
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"create-bucket {b} failed: {c.last_line(res.stderr)}")
        elif re.search(r"\b(403|Forbidden|AccessDenied)\b", err):
            step.die(c.REFUSED, f"Bucket name {b} is already taken by another account (S3 names are global). Pick a different project name.")
        else:
            step.die(c.FAILED, f"head-bucket {b} failed: {err.strip()}")
        # Re-applied every run: cheap, and it repairs a bucket created by hand without them.
        for what, args in (
            (f"block public access on {b}", ["put-public-access-block", "--public-access-block-configuration",
                                             "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"]),
            (f"bucket-owner-enforced ownership on {b}", ["put-bucket-ownership-controls", "--ownership-controls",
                                                         "Rules=[{ObjectOwnership=BucketOwnerEnforced}]"]),
            (f"default SSE-S3 encryption on {b}", ["put-bucket-encryption", "--server-side-encryption-configuration",
                                                   '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}']),
            (f"tag {b} project={project}", ["put-bucket-tagging", "--tagging", f"TagSet=[{{Key=project,Value={project}}}]"]),
        ):
            res = step.apply(what, aws.call, "s3api", args[0], "--bucket", b, *args[1:])
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"{what} failed: {c.last_line(res.stderr)}")


def ensure_exec_role(step, aws, tmp, name):
    """Provided (exists): only verified, never changed. Missing: created. The ARN is always IAM's
    answer; in a dry run of a role that doesn't exist yet it is empty."""
    arn = aws.role_arn(name)
    if arn:
        trust = aws.json("iam", "get-role", "--role-name", name, "--query", "Role.AssumeRolePolicyDocument", "--output", "json")
        if trust is None:
            step.die(c.FAILED, f"get-role {name} failed.")
        attached = aws.text("iam", "list-attached-role-policies", "--role-name", name, "--query",
                            "AttachedPolicies[].PolicyArn", "--output", "text") or ""
        if f"arn:{aws.partition}:iam::aws:policy/AdministratorAccess" not in attached.split():
            step.die(c.REFUSED, f"Execution role {name} exists but doesn't have AdministratorAccess attached. CloudFormation deploys every\n"
                     "   resource with it, so it needs admin. Attach it, or pass --exec-role <another role> (or a new name to create one).")
        if "cloudformation.amazonaws.com" not in json.dumps(trust):
            step.die(c.REFUSED, f"Execution role {name} doesn't trust cloudformation.amazonaws.com, so CloudFormation can't use it as --role-arn.\n"
                     "   Add that service to its trust policy, or pass --exec-role <another role>.")
        step.ok(f"Execution role {name} exists, has AdministratorAccess and trusts CloudFormation.")
        return arn
    doc = os.path.join(tmp, "cfn-trust.json")
    c.write_json(doc, policy.cfn_trust())
    res = step.apply(f"create role {name} (trusted by cloudformation.amazonaws.com)", aws.call,
                     "iam", "create-role", "--role-name", name, "--assume-role-policy-document", f"file://{doc}",
                     "--description", "CloudFormation service role for CRE project deploys", "--query", "Role.Arn", "--output", "text")
    if res is None:
        return ""
    if res.returncode != 0:
        step.die(c.FAILED, f"create-role {name} failed: {c.last_line(res.stderr)}")
    arn = res.stdout.strip()
    res = step.apply(f"attach AdministratorAccess to {name}", aws.call, "iam", "attach-role-policy", "--role-name", name,
                     "--policy-arn", f"arn:{aws.partition}:iam::aws:policy/AdministratorAccess")
    if res is not None and res.returncode != 0:
        step.die(c.FAILED, f"attach-role-policy on {name} failed: {c.last_line(res.stderr)}")
    return arn


def github_oidc_provider(step, aws):
    """The provider's ARN as IAM lists it (or as create returns it), never assembled."""
    listed = aws.text("iam", "list-open-id-connect-providers", "--query", "OpenIDConnectProviderList[].Arn", "--output", "text") or ""
    found = [x for x in listed.split() if x.endswith("/token.actions.githubusercontent.com")]
    if found:
        step.ok("GitHub OIDC provider exists.")
        return found[0]
    res = step.apply("create the GitHub Actions OIDC provider", aws.call, "iam", "create-open-id-connect-provider",
                     "--url", "https://token.actions.githubusercontent.com", "--client-id-list", "sts.amazonaws.com",
                     "--query", "OpenIDConnectProviderArn", "--output", "text")
    if res is None:
        return f"arn:{aws.partition}:iam::{aws.account}:oidc-provider/token.actions.githubusercontent.com"
    if res.returncode != 0:
        step.die(c.FAILED, f"create-open-id-connect-provider failed: {c.last_line(res.stderr)}")
    return res.stdout.strip()


RUNNER_DESCRIPTION = "CI deploy role for CRE projects"   # marks a runner role setup created (and so owns)
TRUST_QUOTA_CODE = "L-C07B4B0D"                          # IAM "Role trust policy length"


def check_trust_size(step, aws, name, doc):
    """IAM refuses a trust policy over the account's limit (2048 characters by default, whitespace
    not counted). Every project a shared runner role trusts adds to it: say so before the call."""
    size = len(json.dumps(doc, separators=(",", ":")))
    r = aws.call("service-quotas", "get-service-quota", "--service-code", "iam", "--quota-code", TRUST_QUOTA_CODE,
                 "--region", "us-east-1", "--query", "Quota.Value", "--output", "text")
    try:
        limit = int(float(r.stdout.strip())) if r.returncode == 0 else 2048
    except ValueError:
        limit = 2048
    if size > limit:
        step.die(c.REFUSED, f"{name}'s trust policy would be {size} characters; this account allows {limit} (IAM quota "
                 f"'Role trust policy length', {TRUST_QUOTA_CODE}). Each project it trusts adds to it. Remove projects that no "
                 "longer exist from its trust, raise the quota (Service Quotas, IAM, up to 4096), or give this project its own "
                 "runner role (--runner-role <new name>; setup creates it).")
    if size > limit * 0.85:
        step.warn(f"{name}'s trust policy is {size} of {limit} characters: a few more projects and IAM refuses it.")


def repair_runner_policy(step, aws, tmp, name, exec_role_arn):
    """A runner role setup created has the account-wide `cre-runner` inline policy. One that lost it
    (or has an older version) gets it back, after a diff and confirmation (the role is shared). A
    role setup didn't create (no RUNNER_DESCRIPTION) is its owner's: only checked, never changed."""
    desc = aws.text("iam", "get-role", "--role-name", name, "--query", "Role.Description", "--output", "text")
    if desc != RUNNER_DESCRIPTION:
        return
    want = policy.runner_policy(aws.account, exec_role_arn)
    r = aws.call("iam", "get-role-policy", "--role-name", name, "--policy-name", "cre-runner", "--query", "PolicyDocument", "--output", "json")
    have = None
    if r.returncode == 0:
        try:
            have = json.loads(r.stdout)
        except ValueError:
            have = None
    elif "NoSuchEntity" not in r.stderr:
        step.die(c.FAILED, f"get-role-policy cre-runner on {name} failed: {c.last_line(r.stderr)}")
    if have == want:
        return
    step.info(f"{name} (created by setup) {'has no' if have is None else 'has an older'} cre-runner policy:")
    print(policy.diff(have or {}, want), file=sys.stderr)
    if not step.confirm(f"Put the current cre-runner policy on {name}?"):
        if not step.dry_run:
            step.die(c.REFUSED, f"Not repairing {name}'s cre-runner policy; CI deploys may fail. Re-run and confirm.")
        return
    pol = os.path.join(tmp, "runner-repair.json")
    c.write_json(pol, want)
    res = step.apply(f"put inline policy cre-runner on {name}", aws.call, "iam", "put-role-policy", "--role-name", name,
                     "--policy-name", "cre-runner", "--policy-document", f"file://{pol}")
    if res is not None and res.returncode != 0:
        step.die(c.FAILED, f"put-role-policy cre-runner on {name} failed: {c.last_line(res.stderr)}")


def ensure_runner_role(step, aws, tmp, a, exec_role_arn, provider_arn, sub_prefix):
    """Provided (exists): only its trust policy changes (this repo added); its permissions are the
    owner's, checked and warned about. Missing: created with the trust and the account-wide
    `cre-runner` policy (no per-project policies)."""
    name = a.runner_role
    arn = aws.role_arn(name)
    current = None
    if arn:
        current = aws.json("iam", "get-role", "--role-name", name, "--query", "Role.AssumeRolePolicyDocument", "--output", "json")
        if current is None:
            step.die(c.FAILED, f"get-role {name} failed.")
        step.info(f"Runner role {name} exists; only its trust policy is checked and, if needed, changed.")
    if a.provider == "gitlab":
        changed, proposed = policy.merge_gitlab_trust(current, a.namespace, a.project)
        exposed = policy.unguarded_projects(current, a.namespace)
        if exposed:
            # Not changed: other projects' pipelines might depend on it. Its owner decides.
            step.warn(f"{name}'s trust also has a statement for {a.namespace} without the protected-branch condition "
                      f"(aws:PrincipalTag/GitLab:Project:ProtectedBranch = Yes), so any branch's job of {', '.join(exposed)} "
                      f"can assume it. {a.project} is trusted only from protected branches. Ask the role's owner to add the "
                      "condition to that statement once those projects deploy only from a protected main.")
            step.out("UNGUARDED_RUNNER_TRUST", " ".join(exposed))
    else:
        changed, proposed = policy.merge_github_trust(current, provider_arn, a.github_repo, sub_prefix)
    trust_file = os.path.join(tmp, "trust-new.json")
    c.write_json(trust_file, proposed)
    if changed:
        check_trust_size(step, aws, name, proposed)
    if current is None:
        res = step.apply(f"create runner role {name}", aws.call, "iam", "create-role", "--role-name", name,
                         "--assume-role-policy-document", f"file://{trust_file}", "--description", RUNNER_DESCRIPTION,
                         "--query", "Role.Arn", "--output", "text")
        if res is None:
            return ""
        if res.returncode != 0:
            step.die(c.FAILED, f"create-role {name} failed: {c.last_line(res.stderr)}")
        arn = res.stdout.strip()
        pol = os.path.join(tmp, "runner.json")
        c.write_json(pol, policy.runner_policy(aws.account, exec_role_arn))
        res = step.apply(f"put inline policy cre-runner on {name} (S3, ECR, CloudFormation, pass {a.exec_role})", aws.call,
                         "iam", "put-role-policy", "--role-name", name, "--policy-name", "cre-runner", "--policy-document", f"file://{pol}")
        if res is not None and res.returncode != 0:
            step.die(c.FAILED, f"put-role-policy cre-runner on {name} failed: {c.last_line(res.stderr)}")
        return arn
    if changed:
        step.info(f"Proposed trust policy change for {name} (shared by every project that uses it):")
        print(policy.diff(current, proposed), file=sys.stderr)
        if step.confirm(f"Update the trust policy of {name}?"):
            res = step.apply(f"update trust policy of {name}", aws.call, "iam", "update-assume-role-policy", "--role-name", name,
                             "--policy-document", f"file://{trust_file}")
            if res is not None and res.returncode != 0:
                step.die(c.FAILED, f"update-assume-role-policy on {name} failed: {c.last_line(res.stderr)}")
        elif not step.dry_run:
            step.die(c.REFUSED, f"Not updating {name}'s trust policy, so CI can't assume it for {a.project}. "
                     "Re-run and confirm, or pass --runner-role <another role>.")
    else:
        step.ok(f"{name} already trusts this project.")
    repair_runner_policy(step, aws, tmp, name, exec_role_arn)
    # Read-only check of what the deploy needs; a gap is the role owner's to fix, so warn, don't change.
    p, r0 = aws.partition, a.regions[0]
    bucket = f"arn:{p}:s3:::{a.project}-{r0}"
    missing = []
    if not aws.can(arn, "s3:PutObject", f"{bucket}/x"):
        missing.append(f"s3:PutObject({bucket}/*)")
    if not aws.can(arn, "s3:ListBucket", bucket):
        missing.append(f"s3:ListBucket({bucket})")
    if not aws.can(arn, "ecr:PutImage", f"arn:{p}:ecr:{r0}:{aws.account}:repository/x"):
        missing.append("ecr:PutImage")
    if not aws.can(arn, "cloudformation:CreateChangeSet", f"arn:{p}:cloudformation:{r0}:{aws.account}:stack/{a.project}/x"):
        missing.append("cloudformation:CreateChangeSet")
    if exec_role_arn and not aws.can(arn, "iam:PassRole", exec_role_arn, "cloudformation.amazonaws.com"):
        missing.append(f"iam:PassRole({exec_role_arn})")
    if missing:
        step.warn(f"{name} may not allow: {' '.join(missing)}. CI deploys will fail until the role's owner grants these.")
        step.out("RUNNER_PERMISSION_GAPS", " ".join(missing))
    else:
        step.ok(f"{name} can upload assets, push images, deploy stacks and pass {a.exec_role}.")
    return arn


def main(step, argv):
    a = parse(step, argv)
    aws = c.Aws(step, a.profile).require()
    tmp = c.tmpdir(step)
    sub_prefix = provider_arn = None
    if a.provider == "github":
        sub_prefix = github_sub_prefix(step, a.github_repo)
    ensure_buckets(step, aws, a.project, a.regions)
    exec_arn = ensure_exec_role(step, aws, tmp, a.exec_role)
    if a.provider == "github":
        provider_arn = github_oidc_provider(step, aws)
    runner_arn = ensure_runner_role(step, aws, tmp, a, exec_arn, provider_arn, sub_prefix)
    if not step.dry_run:
        a.setup_file.set("regions", " ".join(a.regions))   # what check:deploy-prereqs reads
    step.out("ACCOUNT_ID", aws.account)
    step.out("ASSETS_BUCKET_PREFIX", a.project)
    step.out("RUNNER_ROLE_ARN", runner_arn)
    step.out("CFN_EXECUTION_ROLE_ARN", exec_arn)
    step.ok(f"Account {aws.account} is ready for {a.project}.")


if __name__ == "__main__":
    c.cli(NAME, main)
