#!/usr/bin/env python3
"""Check the committed stack plan against the CDK app just synthesized.

    check-stack-plan.py [cre-stacks.json] [cdk.out]

The pipeline is generated from cre-stacks.json, which `npx projen` writes from a synth of the CDK
app (CreApp records every CreStack's region, roles, parameter sources and the outputs other stacks
take, in cdk.out/cre-stacks.json). CI never regenerates files, so a stale plan would deploy with
a stale pipeline. Fails, listing every problem, when:
  - the app's stacks (cdk.out/cre-stacks.json) differ from the ones the committed plan was
    generated from: run `npx projen` and commit;
  - a stack has a fixed environment, isn't a CreStack under a CreApp (no AssetsBucketName /
    AssetsBucketPrefix parameters) or carries CDK metadata;
  - a parameter the plan passes isn't declared, or one without a default isn't passed;
  - an output the plan says later stacks take isn't in the template;
  - a CreIamStack / CreDnsStack template lacks its control-plane-region Rule;
  - a stack's deploy job (`jobs`, exactly what the pipeline job gets) passes other parameters,
    writes other outputs, deploys to another region or under another name, or needs other stacks.

Runs right after `cdk synth` (post-compile, and ci:build:cdk). Exit 0 clean, 1 with a report,
2 when the plan or the cloud assembly is missing.
"""
import json
import pathlib
import sys

ALWAYS_PASSED = {"AssetsBucketName", "AssetsBucketPrefix"}
# The stack kinds whose template must carry a control-plane-region Rule, and the Rule's logical id.
REGION_RULES = {"iam": ("IamControlPlaneRegion", "CreIamStack"), "dns": ("DnsControlPlaneRegion", "CreDnsStack")}
AGNOSTIC = "aws://unknown-account/unknown-region"


def synthesized_stacks(cdk_out):
    manifest = json.loads((cdk_out / "manifest.json").read_text())
    stacks = {}
    for artifact_id, a in manifest.get("artifacts", {}).items():
        if a.get("type") != "aws:cloudformation:stack":
            continue
        props = a.get("properties", {})
        name = props.get("stackName", artifact_id)
        stacks[name] = {
            "environment": a.get("environment"),
            "template": json.loads((cdk_out / props["templateFile"]).read_text()),
        }
    return stacks


def check(plan, stacks, app_plan=None):
    problems = []
    if app_plan is not None and app_plan != plan.get("synthesized"):
        problems.append("the CDK app's stacks changed since cre-stacks.json and the pipeline were generated: "
                        "run npx projen and commit the result.")
    listed = {s["id"]: s for s in plan["stacks"]}
    for missing in sorted(set(listed) - set(stacks)):
        problems.append(f"{missing}: in cre-stacks.json, but the CDK app doesn't create it: run npx projen.")
    for extra in sorted(set(stacks) - set(listed)):
        problems.append(f"{extra}: the CDK app creates it, but cre-stacks.json doesn't list it, so nothing would deploy it: "
                        "run npx projen.")
    for sid in sorted(set(listed) & set(stacks)):
        spec, got = listed[sid], stacks[sid]
        t = got["template"]
        if got["environment"] != AGNOSTIC:
            problems.append(f"{sid}: has a fixed environment ({got['environment']}); remove env from the stack "
                            "(its region prop says where it deploys; use Aws.REGION / Aws.ACCOUNT_ID inside).")
        params = t.get("Parameters", {})
        if not ALWAYS_PASSED <= set(params):
            problems.append(f"{sid}: no AssetsBucketName/AssetsBucketPrefix parameters: make it a CreStack.")
        resources = t.get("Resources", {})
        if "CDKMetadata" in resources or "CheckBootstrapVersion" in t.get("Rules", {}) or any(
                "aws:cdk:path" in (r.get("Metadata") or {}) for r in resources.values() if isinstance(r, dict)):
            problems.append(f"{sid}: carries CDK metadata: create the app with new CreApp() (from @cre/constructs).")
        passed = set(spec["parameters"])
        for p in sorted(passed - set(params)):
            problems.append(f"{sid}: the plan passes parameter {p}, which the template doesn't declare.")
        for p in sorted(set(params) - passed - ALWAYS_PASSED):
            if "Default" not in params[p]:
                problems.append(f"{sid}: parameter {p} has no default and no source; declare it as a CreCfnParameter.")
        rule = REGION_RULES.get(spec.get("kind"))
        if rule and rule[0] not in t.get("Rules", {}):
            problems.append(f"{sid}: a {rule[1]} must carry its {rule[0]} Rule.")
        outputs = set(t.get("Outputs", {}))
        for o in spec["outputs"]:
            if o not in outputs:
                problems.append(f"{sid}: later stacks take output {o}, which the template doesn't have.")
        problems += check_job(sid, spec, plan.get("jobs", {}).get(sid))
    return problems


def check_job(sid, spec, job):
    """The pipeline job's own variables against the plan and the template-checked parameters."""
    if job is None:
        return [f"{sid}: no deploy job in cre-stacks.json; run npx projen."]
    problems = []
    env = job.get("env", {})
    keys = [line.split("=", 1)[0] for line in env.get("STACK_PARAMETERS", "").split("\n") if line]
    if sorted(keys) != sorted(spec["parameters"]):
        problems.append(f"{sid}: its deploy job passes {sorted(keys)}, not the plan's parameters {sorted(spec['parameters'])}.")
    if env.get("OUTPUTS", "").split() != spec["outputs"]:
        problems.append(f"{sid}: its deploy job writes outputs {env.get('OUTPUTS', '').split()}, not the plan's {spec['outputs']}.")
    for k, want in (("STACK_NAME", sid), ("TEMPLATE_NAME", sid), ("AWS_REGION", spec["region"]), ("AWS_DEFAULT_REGION", spec["region"])):
        if env.get(k) != want:
            problems.append(f"{sid}: its deploy job sets {k}={env.get(k)!r}, not {want!r}.")
    if job.get("needs") != spec.get("needs"):
        problems.append(f"{sid}: its deploy job needs {job.get('needs')}, not {spec.get('needs')}.")
    return problems


def main(argv):
    plan_path = pathlib.Path(argv[1] if len(argv) > 1 else "cre-stacks.json")
    cdk_out = pathlib.Path(argv[2] if len(argv) > 2 else "cdk.out")
    if not plan_path.is_file() or not (cdk_out / "manifest.json").is_file():
        print(f"check-stack-plan: need {plan_path} and {cdk_out}/manifest.json; run npx projen and cdk synth first.", file=sys.stderr)
        return 2
    app_plan_file = cdk_out / "cre-stacks.json"
    app_plan = json.loads(app_plan_file.read_text())["stacks"] if app_plan_file.is_file() else None
    if app_plan is None:
        print(f"check-stack-plan: {app_plan_file} is missing: create the app with new CreApp() (from @cre/constructs).", file=sys.stderr)
        return 1
    plan = json.loads(plan_path.read_text())
    problems = check(plan, synthesized_stacks(cdk_out), app_plan)
    if not problems:
        print(f"check-stack-plan: the CDK app matches the {len(plan['stacks'])} stack(s) the pipeline deploys.")
        return 0
    print("check-stack-plan: the CDK app doesn't match cre-stacks.json (the pipeline's plan):", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
