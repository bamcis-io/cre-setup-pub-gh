#!/usr/bin/env python3
"""check:deploy-prereqs — the stacks can deploy with what setup created, before anything merges.

    check-deploy-prereqs.py [cre-stacks.json] [cre-setup.json] [cdk.out]

Offline: compares the stack plan with what setup recorded in cre-setup.json (written by setup and
updated by every `npx projen setup:aws` / `setup:repo`):

  - every stack's region has an assets bucket: it is in `regions` (us-east-1 always has one);
  - every variable a stack needs (`fromVariable`, an assumed or execution role) is a CI/CD variable
    setup set: it is in `ciVariables`. Optional ones only warn.

A gap fails the build with the command that closes it, instead of a green build whose deploy fails
after the merge. No cre-setup.json (a project from before it existed, or the template's own tests):
a warning, not a failure. A cre-setup.json without `ciVariables` (written before this check): only
the regions are checked.
"""
import json
import os
import sys

ALWAYS_BUCKETED = {"us-east-1"}   # setup:aws always makes the us-east-1 bucket (the edge stack)


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        print(f"❌ {path} is unreadable: {e}", file=sys.stderr)
        sys.exit(1)


def secret_variables(plan, cdk_out):
    """{variable: [stack/parameter, ...]} for every noEcho parameter fed from a deploy variable."""
    out = {}
    manifest = os.path.join(cdk_out, "manifest.json") if cdk_out else ""
    if not manifest or not os.path.isfile(manifest):
        return out
    with open(manifest) as f:
        artifacts = json.load(f).get("artifacts", {})
    templates = {}
    for aid, a in artifacts.items():
        if a.get("type") == "aws:cloudformation:stack":
            props = a.get("properties", {})
            templates[props.get("stackName", aid)] = os.path.join(cdk_out, props["templateFile"])
    for s in plan.get("stacks", []):
        path = templates.get(s["id"])
        if not path or not os.path.isfile(path):
            continue
        with open(path) as f:
            params = json.load(f).get("Parameters", {})
        for name, src in (s.get("parameters") or {}).items():
            if src.get("kind") == "env" and str(params.get(name, {}).get("NoEcho", "")).lower() == "true":
                out.setdefault(src["variable"], []).append(f"{s['id']}/{name}")
    return out


def problems(plan, setup, cdk_out=None):
    """(errors, warnings): lists of messages."""
    errors, warnings = [], []
    stacks = plan.get("stacks", [])
    regions = setup.get("regions", "")
    have = set(regions.split() if isinstance(regions, str) else regions) | ALWAYS_BUCKETED
    missing = sorted({s["region"] for s in stacks if s.get("region") and s["region"] not in have})
    if missing:
        users = {r: [s["id"] for s in stacks if s.get("region") == r] for r in missing}
        errors.append("No assets bucket for region(s) " + ", ".join(f"{r} (stacks {', '.join(users[r])})" for r in missing)
                      + ".\n   Create them: npx projen setup:aws   (it adds the regions cre-stacks.json deploys to, and records "
                      "them in cre-setup.json; commit that).")
    known = setup.get("ciVariables")
    if known is not None:
        env = plan.get("environment", {})
        known = set(known)
        req = [v for v in env.get("required", []) if v not in known]
        opt = [v for v in env.get("optional", []) if v not in known]
        if req:
            errors.append("The stacks need CI/CD variable(s) setup never set: " + ", ".join(req) + ".\n   Set them: npx projen setup:repo "
                          "prepare " + " ".join(f"--var {v}=<value>" for v in req) + "   (add :protected for role ARNs; "
                          "it records them in cre-setup.json; commit that).")
        if opt:
            warnings.append("Optional variable(s) not set by setup (the stacks fall back without them): " + ", ".join(opt) + ".")
        hidden = set(setup.get("secretVariables") or [])
        for var, users in sorted(secret_variables(plan, cdk_out).items()):
            if var in known and var not in hidden:
                errors.append(f"{var} feeds a noEcho parameter ({', '.join(users)}), but it is a plain CI/CD variable, shown in job "
                              f"logs.\n   Set it masked: npx projen setup:repo prepare --var {var}=<value>:protected:masked")
    return errors, warnings


def main(argv):
    plan_path = argv[1] if len(argv) > 1 else "cre-stacks.json"
    setup_path = argv[2] if len(argv) > 2 else "cre-setup.json"
    cdk_out = argv[3] if len(argv) > 3 else "cdk.out"
    plan = load(plan_path)
    if plan is None:
        print(f"❌ {plan_path} not found; run npx projen.", file=sys.stderr)
        return 1
    setup = load(setup_path)
    if setup is None:
        print(f"⚠️  No {setup_path}: can't check the stacks against what setup created (regions, CI variables).", file=sys.stderr)
        return 0
    errors, warnings = problems(plan, setup, cdk_out)
    for w in warnings:
        print(f"⚠️  {w}", file=sys.stderr)
    for e in errors:
        print(f"❌ {e}", file=sys.stderr)
    if errors:
        return 1
    print(f"✅ Deploy prerequisites: {len(plan.get('stacks', []))} stack(s) have their buckets"
          + (" and CI variables." if setup.get("ciVariables") is not None else " (CI variables not recorded; not checked)."), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
