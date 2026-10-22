#!/usr/bin/env python3
"""The project's deployed stacks, against the stack plan (cre-stacks.json). Uses your AWS credentials
(the ambient ones, or --profile), in the account setup recorded (cre-setup.json accountId).

  npx projen stacks [--profile p]                     each planned stack's status, and orphans: deployed
                                                      stacks of this project the plan no longer has
                                                      (renamed or removed)
  npx projen stacks:outputs [--stack id] [--profile p]  the deployed stacks' outputs (URLs, ids),
                                                      as `<stack> <Key>=<Value>`
  npx projen teardown [--orphans | --stack id ...] [--profile p] [--yes]
                                                      delete stacks, in reverse deploy order. Without
                                                      --yes it only prints what it would delete (12).
                                                      Default: every planned stack.

A stack is this project's when its name is the project or `<project>-…` AND its AssetsBucketName
parameter is `<project>-<its region>` (what the deploy passes), so another project whose name starts
with this one's is never touched. Stacks with `assumeRole` (another account's DNS) are listed but
never deleted here: delete them with that account's credentials.

Assets buckets and the setup roles are never deleted (setup made them; other projects may share
the roles). Resources a stack retains on delete (a bucket with RemovalPolicy RETAIN) stay.

Exit codes (the setup contract): 0 done | 10 refused (no plan, unknown stack) | 11 credentials or the
wrong account | 12 confirmation needed (--yes) | 20 a deletion failed.
Last stdout line: RESULT code=<n> step=<stacks|outputs|teardown>
"""
import argparse
import json
import os
import subprocess
import sys

OK, REFUSED, CREDENTIALS, CONFIRM, FAILED = 0, 10, 11, 12, 20


class Stop(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def say(msg):
    print(msg, file=sys.stderr, flush=True)


class Aws:
    def __init__(self, profile):
        self.base = ["aws"] + (["--profile", profile] if profile else [])

    def run(self, *args):
        return subprocess.run([*self.base, *args, "--output", "json"], stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def json(self, *args):
        r = self.run(*args)
        if r.returncode != 0:
            raise Stop(FAILED, f"aws {' '.join(args[:2])} failed: {(r.stderr.strip().splitlines() or ['?'])[-1]}")
        return json.loads(r.stdout or "null")

    def account(self):
        r = self.run("sts", "get-caller-identity")
        if r.returncode != 0:
            raise Stop(CREDENTIALS, "AWS credentials missing or expired (sts get-caller-identity failed). Refresh them, or pass --profile.")
        d = json.loads(r.stdout)
        return d["Account"], d["Arn"]

    def describe(self, region, name):
        """The stack, or None when it doesn't exist."""
        r = self.run("cloudformation", "describe-stacks", "--region", region, "--stack-name", name)
        if r.returncode != 0:
            if "does not exist" in r.stderr:
                return None
            raise Stop(FAILED, f"describe-stacks {name} ({region}) failed: {(r.stderr.strip().splitlines() or ['?'])[-1]}")
        return json.loads(r.stdout)["Stacks"][0]


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except ValueError as e:
        raise Stop(REFUSED, f"{path} isn't valid JSON ({e}).")


def context(args):
    plan = load("cre-stacks.json")
    if plan is None:
        raise Stop(REFUSED, "No cre-stacks.json here; run from the project root (npx projen runs there).")
    setup = load("cre-setup.json") or {}
    project = os.environ.get("PROJECT_NAME") or setup.get("project")
    if not project:
        raise Stop(REFUSED, "PROJECT_NAME is not set and cre-setup.json has no project.")
    aws = Aws(args.profile)
    account, arn = aws.account()
    want = setup.get("accountId")
    if want and account != want:
        raise Stop(CREDENTIALS, f"These credentials are for account {account} ({arn}), but the project deploys to {want} "
                   "(cre-setup.json). Use that account's credentials (--profile).")
    regions = setup.get("regions", "")
    regions = regions.split() if isinstance(regions, str) else list(regions)
    stacks = plan.get("stacks", [])
    regions = list(dict.fromkeys(regions + [s["region"] for s in stacks] + ["us-east-1"]))
    return plan, stacks, project, aws, regions, account


def is_ours(stack, project, region):
    name = stack["StackName"]
    if name != project and not name.startswith(project + "-"):
        return False
    params = {p["ParameterKey"]: p.get("ParameterValue") for p in stack.get("Parameters", [])}
    return params.get("AssetsBucketName") == f"{project}-{region}"


def deployed(aws, regions, project):
    """{(name, region): summary} of every stack of this project, in every region it may use."""
    out = {}
    for region in regions:
        token = None
        while True:
            args = ["cloudformation", "describe-stacks", "--region", region]
            if token:
                args += ["--next-token", token]
            d = aws.json(*args)
            for s in d.get("Stacks", []):
                if is_ours(s, project, region):
                    out[(s["StackName"], region)] = s
            token = d.get("NextToken")
            if not token:
                break
    return out


def cmd_status(args):
    plan, stacks, project, aws, regions, account = context(args)
    found = deployed(aws, regions, project)
    planned = {(s["id"], s["region"]) for s in stacks}
    rows = []
    for s in stacks:
        d = found.get((s["id"], s["region"]))
        rows.append((s["id"], s["region"], d["StackStatus"] if d else "NOT_DEPLOYED",
                     (d.get("LastUpdatedTime") or d.get("CreationTime") or "")[:19] if d else "", "other account" if s.get("assumeRole") else ""))
    orphans = sorted(k for k in found if k not in planned)
    say(f"Account {account}, project {project}:")
    w = max([len(r[0]) for r in rows] + [len(o[0]) for o in orphans] + [5])
    for r in rows:
        say(f"  {r[0]:<{w}}  {r[1]:<14} {r[2]:<24} {r[3]}{'  (' + r[4] + ')' if r[4] else ''}")
    if not rows:
        say("  (the plan has no stacks)")
    for name, region in orphans:
        say(f"  {name:<{w}}  {region:<14} {found[(name, region)]['StackStatus']:<24} ORPHAN: deployed, no longer in the plan")
    if orphans:
        say("Orphans keep running (and costing) until deleted: npx projen teardown --orphans  (prints first; --yes deletes).")
    for name, region in orphans:
        print(f"ORPHAN={name}@{region}")
    return OK


def cmd_outputs(args):
    plan, stacks, project, aws, regions, account = context(args)
    want = set(args.stack or [])
    unknown = want - {s["id"] for s in stacks}
    if unknown:
        raise Stop(REFUSED, f"Not in the plan: {', '.join(sorted(unknown))}.")
    n = 0
    for s in stacks:
        if want and s["id"] not in want:
            continue
        if s.get("assumeRole"):
            say(f"{s['id']}: in another account; read its outputs with that account's credentials.")
            continue
        d = aws.describe(s["region"], s["id"])
        if not d:
            say(f"{s['id']}: not deployed.")
            continue
        for o in sorted(d.get("Outputs", []), key=lambda o: o["OutputKey"]):
            print(f"{s['id']} {o['OutputKey']}={o['OutputValue']}")
            n += 1
    say(f"{n} output(s).")
    return OK


def delete(aws, name, region):
    """Delete and wait. Returns "" when gone, else why not."""
    r = aws.run("cloudformation", "delete-stack", "--region", region, "--stack-name", name)
    if r.returncode != 0:
        return (r.stderr.strip().splitlines() or ["delete-stack failed"])[-1]
    w = aws.run("cloudformation", "wait", "stack-delete-complete", "--region", region, "--stack-name", name)
    if w.returncode == 0:
        return ""
    ev = aws.run("cloudformation", "describe-stack-events", "--region", region, "--stack-name", name)
    reasons = []
    if ev.returncode == 0:
        for e in json.loads(ev.stdout).get("StackEvents", [])[:30]:
            if e.get("ResourceStatus") == "DELETE_FAILED":
                reasons.append(f"{e['LogicalResourceId']}: {e.get('ResourceStatusReason', '')}")
    return "DELETE_FAILED" + (": " + "; ".join(reasons[:5]) if reasons else "")


def cmd_teardown(args):
    plan, stacks, project, aws, regions, account = context(args)
    found = deployed(aws, regions, project)
    planned_ids = [s["id"] for s in stacks]
    if args.orphans and args.stack:
        raise Stop(REFUSED, "Give --orphans or --stack, not both.")
    unknown = set(args.stack or []) - set(planned_ids)
    if unknown:
        raise Stop(REFUSED, f"Not in the plan: {', '.join(sorted(unknown))} (orphans: --orphans).")
    targets, skipped = [], []
    if args.orphans:
        planned = {(s["id"], s["region"]) for s in stacks}
        targets = sorted(k for k in found if k not in planned)
    else:
        for s in reversed(stacks):   # the reverse of the deploy order: dependents first
            if args.stack and s["id"] not in args.stack:
                continue
            if s.get("assumeRole"):
                skipped.append(s["id"])
            elif (s["id"], s["region"]) in found:
                targets.append((s["id"], s["region"]))
    for sid in skipped:
        say(f"Skipping {sid}: it deploys to another account (assumeRole). Delete it there: "
            f"aws cloudformation delete-stack --stack-name {sid} --profile <that account>")
    if not targets:
        say("Nothing to delete.")
        return OK
    say(f"Account {account}: {'would delete' if not args.yes else 'deleting'}, in this order:")
    for name, region in targets:
        say(f"  {name} ({region}, {found[(name, region)]['StackStatus']})")
    if not args.yes:
        say("Re-run with --yes to delete them. Their resources go too, except what a stack retains; buckets and setup roles stay.")
        return CONFIRM
    failed = []
    for name, region in targets:
        say(f"Deleting {name} ({region})...")
        why = delete(aws, name, region)
        if why:
            failed.append(f"{name}: {why}")
            say(f"  ❌ {why}")
            if not args.orphans:
                break   # a dependent left in place blocks the stacks it needs
        else:
            say("  ✅ deleted")
    if failed:
        raise Stop(FAILED, "Not deleted: " + "; ".join(failed) + ". Fix the resource named, then re-run (deleted stacks are skipped).")
    return OK


def main(argv):
    p = argparse.ArgumentParser(prog="stacks.py")
    p.add_argument("command", choices=["status", "outputs", "teardown"])
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    p.add_argument("--stack", action="append")
    p.add_argument("--orphans", action="store_true")
    p.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    step = {"status": "stacks", "outputs": "outputs", "teardown": "teardown"}[args.command]
    try:
        code = {"status": cmd_status, "outputs": cmd_outputs, "teardown": cmd_teardown}[args.command](args)
    except Stop as s:
        say(f"❌ {s.msg}")
        code = s.code
    print(f"RESULT code={code} step={step}")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
