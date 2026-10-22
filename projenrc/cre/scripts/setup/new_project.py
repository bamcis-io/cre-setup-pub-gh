#!/usr/bin/env python3
"""Create a CRE project end to end from an answers file: repo, AWS account, DNS, CI settings,
scaffold, green build, first push. No decisions are left to whoever runs it.

  new_project.py --answers <file> [--yes] [--wait <seconds>] [--from <phase>]

The answers come from /cre-demo-init's questions (recipe/recipe.md STEP 1), written to
<template>/.agents/projects/<name>.json (`.agents/` is never committed to the template). The file
is checked strictly (ANSWERS below); anything unknown or missing is refused (exit 10) before
anything is created.

Phases, always in this order (each one is idempotent; a re-run repeats the setup phases, which
change nothing when already done, and skips the scaffold and build once they succeeded):

  check        answers, tools, credentials (AWS profile; gh login or Midway), the demo directory
  repo-create  the GitHub repo / GitLab project, private (GitHub: the runner role trusts the repo's
               OIDC subject, which only exists once the repo does)
  github-app   GitHub: the builder's App aws-cre-automation-<alias>, created once (a browser click)
  aws          buckets, CloudFormation execution role, runner role trust (aws_account.py)
  dns          only with `dns` in the answers (dns.py)
  repo-config  CI/CD variables or Actions secrets, and on GitHub the App's secrets and installation
  scaffold     <template>/../<name>: the project type, skeleton (no stacks), constructs, skill links,
               placeholders, cre-setup.json, git init
  build        bootstrap projen and build green (synth, tests, lint, package)
  push         the first commit pushed to main, then main protected (repo_*.py finalize)

Exit codes are the setup contract's (crelib.py): 0 done; 10 refused; 11 credentials (the message
says what to refresh); 12 a shared role's trust would change (show the diff, re-run with --yes once
the builder agrees); 13 a person must act on each NEEDS= line (a browser click), everything else
done, re-run the same command afterwards; 20 failed (fix the cause, re-run). A 13 doesn't stop the
run: the phases that don't depend on it go on, so one re-run finishes. Every other non-zero code
stops at that phase.

--wait N: how long the GitHub App creation and installation wait for the browser click before
reporting NEEDS (default 0 without a terminal, so an agent gets the URLs back at once; 600 at one).
--from <phase>: re-run the scaffold or build even though they succeeded before.
Prints DEMO_DIR, REPO_WEB_URL and any NEEDS= lines; the last line is RESULT.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

NAME = "new-project"
SETUP = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(os.path.dirname(SETUP))          # packages/cre-projen
TEMPLATE = os.path.dirname(os.path.dirname(PKG))       # the cre-demo-template clone
PHASES = ["check", "repo-create", "github-app", "aws", "dns", "repo-config", "scaffold", "build", "push"]

PROJECT_TYPES = {"CreResilienceDemo": "standard", "CreFailureScenario": "failure-scenario", "CreCustomerSandbox": "standard"}
DEFAULT_BINDLE = "amzn1.bindle.resource.v4o66fxvd3zfcqvcmhxq"
REGION_RE = r"[a-z]{2,4}(-[a-z]+)+-\d"   # us-east-2, us-gov-west-1, eusc-de-east-1
# What the scaffold is made from (the project's files and cre-setup.json). Changing any of these
# after the scaffold exists is refused; the rest (awsProfile, gitlab.tokenFile, dns) only feed the
# setup phases, which re-run every time.
SCAFFOLD_KEYS = ("name", "description", "projectType", "provider", "github", "regions", "runnerRole", "execRole",
                 "bindleId", "forbiddenTerms")


def load_step(module):
    """A sibling setup step's module, loaded by path (a name like `dns` could clash with a package)."""
    spec = importlib.util.spec_from_file_location(f"cre_setup_{module}", os.path.join(SETUP, f"{module}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- answers -------------------------------------------------------------------------------------

def check_answers(step, d):
    """The answers, checked strictly: every key known, every required key present, each value valid.
    Returns the normalized answers."""
    errors = []

    def err(msg):
        errors.append(msg)

    def allowed(obj, where, keys):
        for k in obj:
            if k not in keys:
                err(f"{where}{k}: unknown key (allowed: {', '.join(sorted(keys))})")

    if not isinstance(d, dict):
        step.die(c.REFUSED, "The answers file must hold a JSON object.")
    allowed(d, "", {"name", "description", "projectType", "provider", "github", "gitlab", "regions", "awsProfile",
                    "runnerRole", "execRole", "bindleId", "dns", "forbiddenTerms"})
    name = d.get("name", "")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,40}[a-z0-9]", name):
        err("name: 3-42 lowercase letters, digits or hyphens (it prefixes S3 bucket names)")
    desc = d.get("description", "")
    if not isinstance(desc, str) or not desc.strip() or "\n" in desc:
        err("description: one non-empty line")
    if d.get("projectType") not in PROJECT_TYPES:
        err(f"projectType: one of {', '.join(PROJECT_TYPES)}")
    provider = d.get("provider")
    if provider not in ("github", "gitlab"):
        err("provider: github or gitlab")
    regions = d.get("regions")
    if not isinstance(regions, list) or not regions or not all(isinstance(r, str) and re.fullmatch(REGION_RE, r) for r in regions) \
            or len(set(regions)) != len(regions):
        err("regions: a non-empty list of distinct AWS regions, the first the one the project deploys to (e.g. [\"us-east-2\"])")
    if not isinstance(d.get("awsProfile"), str) or not d.get("awsProfile"):
        err("awsProfile: the AWS profile for the target account")
    for k in ("runnerRole", "execRole"):
        if k in d and not (isinstance(d[k], str) and re.fullmatch(r"[\w+=,.@-]{1,64}", d[k])):
            err(f"{k}: an IAM role name")
    if "bindleId" in d and not (isinstance(d["bindleId"], str) and d["bindleId"].startswith("amzn1.bindle.")):
        err("bindleId: an amzn1.bindle... id")
    if provider == "github":
        if "gitlab" in d:
            err("gitlab: not used with provider github")
        g = d.get("github")
        if not isinstance(g, dict):
            err("github: {\"org\": <owner>, \"alias\": <Amazon alias>}")
        else:
            allowed(g, "github.", {"org", "alias"})
            if not (isinstance(g.get("org"), str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", g.get("org", ""))):
                err("github.org: the GitHub owner (organization or user) of the repo")
            if not (isinstance(g.get("alias"), str) and re.fullmatch(r"[a-z][a-z0-9-]{1,14}", g.get("alias", ""))):
                err("github.alias: the builder's Amazon alias (their App is aws-cre-automation-<alias>)")
    elif provider == "gitlab":
        if "github" in d:
            err("github: not used with provider gitlab")
        g = d.get("gitlab", {})
        if not isinstance(g, dict):
            err("gitlab: {\"namespace\": <group/path>, \"tokenFile\": <optional path>}")
        else:
            allowed(g, "gitlab.", {"namespace", "tokenFile"})
            ns = g.get("namespace", "aws-cre/resilience-demos")
            if not (isinstance(ns, str) and re.fullmatch(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*", ns)):
                err("gitlab.namespace: a GitLab group path")
            if "tokenFile" in g and not isinstance(g["tokenFile"], str):
                err("gitlab.tokenFile: a path")
    terms = d.get("forbiddenTerms")
    if terms is not None:
        if d.get("projectType") != "CreFailureScenario":
            err("forbiddenTerms: only for CreFailureScenario")
        elif not (isinstance(terms, list) and all(isinstance(t, str) and t.strip() for t in terms)):
            err("forbiddenTerms: a list of non-empty strings")
    dns = d.get("dns")
    if dns is not None:
        if not isinstance(dns, dict):
            err("dns: an object")
        else:
            allowed(dns, "dns.", {"zoneId", "domain", "dnsProfile", "supernova", "alias", "zoneName"})
            if bool(dns.get("zoneId")) == bool(dns.get("supernova")):
                err("dns: exactly one of zoneId (an existing hosted zone) or supernova (people | cre: a new zone)")
            if dns.get("supernova") not in (None, "people", "cre"):
                err("dns.supernova: people or cre")
            if dns.get("supernova") == "people" and not dns.get("alias"):
                err("dns.alias: required with supernova people")
            if dns.get("supernova") == "cre" and not dns.get("zoneName"):
                err("dns.zoneName: required with supernova cre")
            for k in ("zoneId", "domain", "dnsProfile", "alias", "zoneName"):
                if k in dns and not isinstance(dns[k], str):
                    err(f"dns.{k}: a string")
    if errors:
        step.die(c.REFUSED, "The answers file has problems:\n   " + "\n   ".join(errors))
    a = dict(d)
    a["layout"] = PROJECT_TYPES[a["projectType"]]
    a["awsRegion"] = a["regions"][0]
    if provider == "gitlab":
        a["gitlab"] = {"namespace": "aws-cre/resilience-demos", **(d.get("gitlab") or {})}
    return a


# ---- state ---------------------------------------------------------------------------------------

class State:
    """Progress beside the answers file (<name>.state.json): which of scaffold/build succeeded, the
    answers they were made from, and the values setup produced (for the record)."""

    def __init__(self, step, path, answers_hash):
        self.path = path
        self.data = {"answersHash": answers_hash, "done": [], "outputs": {}}
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if not isinstance(loaded, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                step.die(c.REFUSED, f"The state file {path} is unreadable ({e}). Delete it to start over (the setup phases are "
                         "idempotent; a scaffolded project directory must then be moved away first).")
            self.data.update(loaded)
            self.data.setdefault("done", [])
            self.data.setdefault("outputs", {})
        self.answers_hash = answers_hash

    def done(self, phase):
        return phase in self.data["done"]

    def mark(self, phase):
        if phase not in self.data["done"]:
            self.data["done"].append(phase)
        self.data["answersHash"] = self.answers_hash
        self.save()

    def forget(self, phases):
        self.data["done"] = [p for p in self.data["done"] if p not in phases]
        self.save()

    def record(self, outputs):
        self.data["outputs"].update(outputs)
        self.save()

    def save(self):
        c.write_json(self.path, self.data)


# ---- running the setup steps ---------------------------------------------------------------------

class Run:
    def __init__(self, step, a, state, args):
        self.step, self.a, self.state, self.args = step, a, state, args
        self.values = {}
        # Beside the template clone (the layout the skill links and update-template expect).
        # CRE_PROJECTS_DIR puts it elsewhere: the offline harness uses it.
        self.demo = os.path.join(os.environ.get("CRE_PROJECTS_DIR") or os.path.dirname(TEMPLATE), a["name"])
        if a["provider"] == "github":
            self.repo = f"{a['github']['org']}/{a['name']}"
            self.app_slug = f"aws-cre-automation-{a['github']['alias']}"
        else:
            self.repo = f"{a['gitlab']['namespace']}/{a['name']}"
            self.app_slug = ""
        self.needs = {}          # phase key -> NEEDS its step reported last
        self.step_outputs = {}   # phase key -> that step's outputs

    def report(self):
        """What the builder must still do, printed once at the end (also after a stop), from each
        step's latest check: NEEDS lines, the page each one refers to, permission gaps."""
        needs = []
        for ns in self.needs.values():
            needs += [n for n in ns if n not in needs]
        if "APP_CREATE" in needs and "PROJEN_CREDENTIALS" in needs:
            needs.remove("PROJEN_CREDENTIALS")   # set as soon as the App exists (a re-run)
        for n in needs:
            self.step.need(n)
        outs = {}
        for o in self.step_outputs.values():
            outs.update(o)
        for k, need in (("APP_CREATE_URL", "APP_CREATE"), ("APP_INSTALL_URL", "APP_INSTALL")):
            if need in needs and outs.get(k):
                self.step.out(k, outs[k])
        if outs.get("RUNNER_PERMISSION_GAPS"):
            self.step.out("RUNNER_PERMISSION_GAPS", outs["RUNNER_PERMISSION_GAPS"])

    def setup_step(self, module, name, argv, key):
        """Run a setup step in this process; returns (code, outputs). Its NEEDS are recorded under
        `key`, replacing what an earlier call under the same key reported (a step checked again
        later in the run, after the builder clicked, clears its NEEDS). A 13 lets the run go on; any
        other failure ends it with the step's own code (its message is already shown)."""
        code, s = c.invoke(name, load_step(module).main, argv, echo=False)
        self.needs[key] = list(s.needs)
        self.step_outputs[key] = dict(s.outputs)
        if code not in (c.OK, c.NEEDS):
            self.step.info(f"Stopped at {name} (exit {code}). Fix what it says, then re-run the same command.")
            raise c.Exit(code)
        self.values.update(s.outputs)
        self.state.record({k: v for k, v in s.outputs.items() if not k.startswith("APP_")})
        return code, s.outputs

    def flags(self):
        return ["--yes"] if self.args.yes else []

    def owns_demo(self):
        """This command created the directory (recorded by its real path, so a changed name or
        projects directory never matches someone else's)."""
        return self.state.data.get("demoCreated") == os.path.realpath(self.demo)


def phase_check(r):
    step, a = r.step, r.a
    if not (os.path.isdir(os.path.join(TEMPLATE, "skeleton")) and os.path.isdir(os.path.join(TEMPLATE, "constructs"))):
        step.die(c.REFUSED, f"{TEMPLATE} isn't a cre-demo-template clone: run new_project.py from the template's packages/cre-projen/scripts/setup.")
    if not shutil.which("yarn"):
        # Homebrew's Node 25+ ships without corepack, which provides yarn.
        fix = "corepack enable" if shutil.which("corepack") else "npm install -g corepack && corepack enable"
        step.die(c.REFUSED, f"yarn isn't installed (projects use Yarn Berry). Run: {fix}   then re-run.")
    c.require_tools(step, "git", "aws", "node", "npm", "npx", "zip", "unzip", "rsync", "curl")
    if a["provider"] == "github":
        c.require_tools(step, "gh", "openssl")
        c.require_gh(step)
        login = c.gh("api", "user", "--jq", ".login").stdout.strip()
        if not login:
            step.die(c.FAILED, "Could not read your GitHub login (gh api user).")
        r.values["GH_USERNAME"] = login
    else:
        c.require_tools(step, "ssh")
        gl = c.GitLab(step)
        gl.require_midway()
        record_gitlab_token(r)
    c.require_git_identity(step)
    # The AWS profile (and the production-account confirmation), before anything is created anywhere.
    c.Aws(step, a["awsProfile"]).require()
    # The demo goes beside the template, never inside it, and never over something else.
    if os.path.realpath(r.demo).startswith(os.path.realpath(TEMPLATE) + os.sep):
        step.die(c.REFUSED, f"{r.demo} would be inside the template clone; it must be a sibling.")
    if os.path.exists(r.demo) and not r.owns_demo():
        if not os.path.isdir(r.demo) or os.listdir(r.demo):
            step.die(c.REFUSED, f"{r.demo} already exists and isn't a project this command created. Pick another name, or move it away.")


def record_gitlab_token(r):
    """The GitLab token file: the answers' gitlab.tokenFile, else $CRE_GITLAB_TOKEN_FILE, else the
    one the template records. Its location (never the token) is recorded in the template, as the
    link .agents/gitlab-token, so a later project needn't ask; each project links it at
    .cre/gitlab-token (link_project_token)."""
    step = r.step
    given = r.a["gitlab"].get("tokenFile") or os.environ.get("CRE_GITLAB_TOKEN_FILE", "")
    path = c.expand_path(given) if given else c.recorded_token_target()
    if not path:
        step.die(c.REFUSED, "No GitLab token file: the answers have no gitlab.tokenFile, and none is recorded from an earlier "
                 "project. /cre-demo-init asks for it (STEP 1, 4b).\n" + c.token_help())
    if not os.path.lexists(path):
        step.die(c.CREDENTIALS, f"The GitLab token file {path}{f' (given as {given})' if given and given != path else ''} doesn't exist.\n"
                 + c.token_help())
    token, err = c.read_token_file(path)
    if err:
        step.die(c.CREDENTIALS, f"The GitLab token file {path} {err}.\n" + c.token_help())
    # The file holds a token GitLab accepts, before anything is created with it.
    c.check_gitlab_token(step, token, path)
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o077:
        step.warn(f"{path} can be read by other users (mode {oct(mode)}). Run: chmod 600 {path}")
    rec = c.template_token_record()
    state = c.place_link(rec, path)
    if state == "blocked":
        step.die(c.REFUSED, f"{rec} exists and isn't a link. It must be a link to the token file (never the token): move it away, re-run.")
    if state != "unchanged":
        step.ok(f"Recorded where the GitLab token file is ({rec} → {path}); later projects reuse it.")
    os.environ["CRE_GITLAB_TOKEN_FILE"] = path
    r.token_file = path


def link_project_token(r):
    """<project>/.cre/gitlab-token → the token file (.cre/ is gitignored). The setup tasks and the git
    tasks read the token through it."""
    if r.a["provider"] != "gitlab" or not os.path.isdir(r.demo) or not getattr(r, "token_file", ""):
        return
    link = os.path.join(r.demo, c.PROJECT_TOKEN_LINK)
    state = c.place_link(link, r.token_file)
    if state == "blocked":
        r.step.warn(f"{link} is a real file, not a link; left as is. It should be a link to the token file: "
                    f"rm {link} && ln -s {r.token_file} {link}")
    elif state != "unchanged":
        r.step.ok(f"Linked {c.PROJECT_TOKEN_LINK} → {r.token_file} in the project.")


def phase_repo_create(r):
    if r.a["provider"] == "github":
        r.setup_step("repo_github", "repo-github", ["prepare", "--repo", r.repo, "--wait-install", "0"], "repo-create")
    else:
        r.setup_step("repo_gitlab", "repo-gitlab", ["prepare", "--namespace", r.a["gitlab"]["namespace"], "--project", r.a["name"]]
                     + bindle_flag(r), "repo-create")


def bindle_flag(r):
    return ["--bindle", r.a["bindleId"]] if r.a.get("bindleId") else []


def phase_github_app(r):
    if r.a["provider"] != "github":
        return
    # Never waits here: creating the App is a click the builder can make while the account is set
    # up. repo-config, the first phase that needs the App, checks again (and waits for the click).
    check_app(r, 0)
    if not r.values.get("APP_READY"):
        r.step.info("Carrying on; the App is checked again before it's needed (repo-config).")


def check_app(r, wait):
    """github_app.py: the App ready (it exists, it's yours, its key checks out; it may still lack a
    permission: NEEDS=APP_PERMISSIONS), or NEEDS=APP_CREATE with the local page running."""
    _, out = r.setup_step("github_app", "github-app", ["--alias", r.a["github"]["alias"], "--wait", str(wait)], "github-app")
    r.values["APP_READY"] = "1" if out.get("APP_ID") else ""
    if out.get("APP_CREATE_OPENED"):
        r.values["APP_CREATE_OPENED"] = "1"


def click_wait(r):
    """How long to wait for a browser click: --wait when given; otherwise 600 s when the page opened
    in a browser on this machine (someone is there to click it), else 0 (the URL must reach them)."""
    if r.args.wait is not None:
        return r.args.wait
    return 600 if r.values.get("APP_CREATE_OPENED") else 0


def phase_aws(r):
    a = r.a
    argv = ["--profile", a["awsProfile"], "--project", a["name"], "--regions", " ".join(a["regions"]), "--provider", a["provider"]]
    argv += ["--namespace", a["gitlab"]["namespace"]] if a["provider"] == "gitlab" else ["--github-repo", r.repo]
    if a.get("runnerRole"):
        argv += ["--runner-role", a["runnerRole"]]
    if a.get("execRole"):
        argv += ["--exec-role", a["execRole"]]
    r.setup_step("aws_account", "aws-account", argv + r.flags(), "aws")


def phase_dns(r):
    dns = r.a.get("dns")
    if not dns:
        return
    a = r.a
    argv = ["--profile", a["awsProfile"], "--project", a["name"], "--regions", " ".join(a["regions"]), "--provider", a["provider"]]
    if a.get("runnerRole"):
        argv += ["--runner-role", a["runnerRole"]]
    if dns.get("dnsProfile"):
        argv += ["--dns-profile", dns["dnsProfile"]]
    if dns.get("zoneId"):
        argv += ["--zone-id", dns["zoneId"]] + (["--domain", dns["domain"]] if dns.get("domain") else [])
    else:
        argv += ["--supernova", dns["supernova"]]
        argv += ["--alias", dns["alias"]] if dns.get("alias") else []
        argv += ["--zone-name", dns["zoneName"]] if dns.get("zoneName") else []
    if a.get("bindleId"):
        argv += ["--bindle", a["bindleId"]]
    r.setup_step("dns", "dns", argv + r.flags(), "dns")


def deploy_variables(r):
    """(name, value, protected) for the deploy's CI/CD variables / secrets."""
    v = r.values
    out = [("AWS_CREDS_TARGET_ROLE", v["RUNNER_ROLE_ARN"], True), ("CFN_EXECUTION_ROLE_ARN", v["CFN_EXECUTION_ROLE_ARN"], True),
           ("ASSETS_BUCKET_PREFIX", r.a["name"], False)]
    if r.a.get("bindleId"):
        out.append(("BINDLE_ID", r.a["bindleId"], False))
    if v.get("DOMAIN") and v.get("DELEGATION") != "pending":
        out += [("DOMAIN", v["DOMAIN"], False), ("HOSTED_ZONE_ID", v["HOSTED_ZONE_ID"], False)]
    if v.get("DNS_ROLE_ARN"):
        out += [("DNS_ROLE_ARN", v["DNS_ROLE_ARN"], True), ("DNS_CFN_EXECUTION_ROLE_ARN", v["DNS_CFN_EXECUTION_ROLE_ARN"], True)]
    return out


def phase_repo_config(r):
    for k in ("RUNNER_ROLE_ARN", "CFN_EXECUTION_ROLE_ARN"):
        if not r.values.get(k):
            r.step.die(c.FAILED, f"The aws phase produced no {k}; re-run.")
    if r.a["provider"] == "github":
        if not r.values.get("APP_READY"):
            # The builder may have clicked Create since the github-app phase: check again, waiting
            # for the click when the page opened in their browser.
            check_app(r, click_wait(r))
        argv = ["prepare", "--repo", r.repo]
        # Without --wait, repo_github.py waits for the install click only when its page opened here.
        argv += ["--wait-install", str(r.args.wait)] if r.args.wait is not None else []
        for k, val, _ in deploy_variables(r):
            argv += ["--secret", f"{k}={val}"]
        if r.values.get("APP_READY"):
            argv += ["--app-slug", r.app_slug]
        r.setup_step("repo_github", "repo-github", argv, "repo-config")
    else:
        argv = ["prepare", "--namespace", r.a["gitlab"]["namespace"], "--project", r.a["name"]] + bindle_flag(r)
        for k, val, prot in deploy_variables(r):
            argv += ["--var", f"{k}={val}{':protected' if prot else ''}"]
        r.setup_step("repo_gitlab", "repo-gitlab", argv, "repo-config")


# ---- scaffold ------------------------------------------------------------------------------------

TOKENS = ("__PROJECT_TYPE__", "__PROVIDER_OPTIONS__", "__REPOSITORY__", "__DEMO_NAME__", "__DEMO_DESCRIPTION__",
          "__EXTRA_OPTIONS__", "__AWS_REGION__")
SKIP_DIRS = {"projenrc", "cre-constructs", "node_modules", ".git", ".kiro", ".claude", "cdk.out"}


def ts_string(s):
    """A TypeScript string literal (JSON's escaping is valid TypeScript)."""
    return json.dumps(s)


def substitutions(r):
    a = r.a
    extra = []
    if a.get("forbiddenTerms"):
        extra.append(f"forbiddenTerms: [{', '.join(ts_string(t) for t in a['forbiddenTerms'])}],")
    if a.get("bindleId") and a["bindleId"] != DEFAULT_BINDLE:
        extra.append(f"bindleId: {ts_string(a['bindleId'])},")
    if a["provider"] == "gitlab":
        provider = f"gitlab: {{ awsRegion: {ts_string(a['awsRegion'])} }}"
        repository = f"https://{c.GITLAB_HOST}/{r.repo}"
    else:
        provider = (f"github: {{ username: {ts_string(r.values['GH_USERNAME'])}, awsRegion: {ts_string(a['awsRegion'])}, "
                    f"appSlug: {ts_string(r.app_slug)} }}")
        repository = f"https://github.com/{r.repo}"
    # The skeleton quotes the name, repository and description itself ('…' / "…"): substitute the
    # escaped body of a string literal there, so a quote in a description can't break the file.
    body = lambda s: ts_string(s)[1:-1]  # noqa: E731
    return {
        "__PROJECT_TYPE__": a["projectType"],
        "__PROVIDER_OPTIONS__": provider,
        "__REPOSITORY__": body(repository),
        "__DEMO_NAME__": a["name"],
        "__DEMO_DESCRIPTION__": body(a["description"]),
        "__EXTRA_OPTIONS__": " ".join(extra),
        "__AWS_REGION__": a["awsRegion"],
    }


def scaffold_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS]
        for f in filenames:
            if f.endswith((".ts", ".md")):
                yield os.path.join(dirpath, f)


def copy_tree(src, dst):
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))


def run_script(step, what, cmd, cwd=None):
    res = c.run(cmd, cwd=cwd)
    if res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        step.die(c.FAILED, f"{what} failed (exit {res.returncode}).")
    return res


def phase_scaffold(r):
    step, a, demo = r.step, r.a, r.demo
    if os.path.exists(demo) and os.listdir(demo):
        # A scaffold that failed part way, or --from scaffold before the first push: start it again
        # cleanly, but only in a directory this command created (phase check enforces it).
        if not r.owns_demo() or r.state.done("push"):
            step.die(c.REFUSED, f"{demo} exists and can't be re-scaffolded. Move it away to start over.")
        step.info(f"Removing the unpushed scaffold at {demo} from an earlier run.")
        shutil.rmtree(demo)
    os.makedirs(demo, exist_ok=True)
    r.state.data["demoCreated"] = os.path.realpath(demo)
    r.state.save()
    step.plan(f"scaffold {a['projectType']} ({a['layout']} layout) at {demo}")
    for d in ("src", "scripts", "steering"):
        copy_tree(os.path.join(PKG, d), os.path.join(demo, "projenrc", "cre", d))
    skel = os.path.join(TEMPLATE, "skeleton")
    copy_tree(os.path.join(skel, "_shared"), demo)
    copy_tree(os.path.join(skel, f"layout-{a['layout']}"), demo)
    shutil.copyfile(os.path.join(skel, ".projenrc.ts"), os.path.join(demo, ".projenrc.ts"))
    # Before attach-template.sh, which reads the provider from it.
    c.write_json(os.path.join(demo, "cre-setup.json"), setup_record(r), sort_keys=False)
    run_script(step, "Linking the template (attach-template.sh)",
               ["bash", os.path.join(PKG, "scripts", "template", "attach-template.sh"), TEMPLATE, demo])
    run_script(step, "Installing the construct library (sync-constructs.sh)",
               ["bash", os.path.join(PKG, "scripts", "template", "sync-constructs.sh"), TEMPLATE, os.path.join(demo, "cre-constructs")])
    subs = substitutions(r)
    # One pass, so a value containing a token (a description mentioning __EXTRA_OPTIONS__) stays text.
    pattern = re.compile("|".join(re.escape(t) for t in TOKENS))
    unknown = set()
    for f in scaffold_files(demo):
        with open(f) as fh:
            text = fh.read()
        # A placeholder the skeleton has but this script doesn't fill (checked before substituting,
        # so a value that happens to look like one isn't mistaken for it).
        unknown |= {t for t in re.findall(r"__[A-Z][A-Z_]*[A-Z]__", text) if t not in TOKENS}
        new = pattern.sub(lambda m: subs[m.group(0)], text)
        if new != text:
            with open(f, "w") as fh:
                fh.write(new)
    if unknown:
        step.die(c.FAILED, f"The skeleton has placeholders new_project.py doesn't fill: {', '.join(sorted(unknown))}")
    # The branch must be `main`: the git tasks, projen's defaultReleaseBranch and the workflows target it.
    run_script(step, "git init", ["git", "init", "-q"], cwd=demo)
    run_script(step, "git symbolic-ref", ["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=demo)
    step.ok(f"Scaffolded {demo}.")


def setup_record(r):
    """cre-setup.json: what setup was given and produced (no secrets, no per-machine profile names),
    read back by the project's `npx projen setup:*` tasks."""
    a, v = r.a, r.values
    record = {
        "projectType": a["projectType"], "provider": a["provider"], "project": a["name"],
        "namespace": a["gitlab"]["namespace"] if a["provider"] == "gitlab" else "",
        "githubRepo": r.repo if a["provider"] == "github" else "", "regions": " ".join(a["regions"]),
        "accountId": v.get("ACCOUNT_ID", ""),
        "runnerRole": a.get("runnerRole") or ("GitLabRunner" if a["provider"] == "gitlab" else "CreGitHubDeploy"),
        "runnerRoleArn": v.get("RUNNER_ROLE_ARN", ""), "execRole": a.get("execRole") or "CreCfnExecution", "cfnExecutionRoleArn": v.get("CFN_EXECUTION_ROLE_ARN", ""), "appSlug": r.app_slug,
        "githubUsername": v.get("GH_USERNAME", ""), "bindleId": a.get("bindleId", ""),
        # How PRs merge (githubMergeMode): private → the build workflow merges them.
        "githubVisibility": v.get("REPO_VISIBILITY", "private") if a["provider"] == "github" else "",
        "domain": v.get("DOMAIN", "") if v.get("DELEGATION") != "pending" else "", "hostedZoneId": v.get("HOSTED_ZONE_ID", ""),
        "dnsRoleArn": v.get("DNS_ROLE_ARN", ""),
        # Names only: what check:deploy-prereqs compares with the variables the stacks need.
        "ciVariables": sorted(k for k, _, _ in deploy_variables(r)) if v.get("RUNNER_ROLE_ARN") else [],
        "secretVariables": sorted(k for k, _, _ in deploy_variables(r)) if a["provider"] == "github" and v.get("RUNNER_ROLE_ARN") else [],
    }
    return {k: val for k, val in record.items() if val}


def refresh_record(r):
    """A re-run after the scaffold (new DNS, a new role ARN): bring cre-setup.json up to date. It is
    committed and main takes no pushes, so the change goes in through a PR/MR."""
    path = os.path.join(r.demo, "cre-setup.json")
    try:
        with open(path) as f:
            current = json.load(f)
    except (OSError, ValueError):
        current = None
    new = setup_record(r)
    if current == new:
        return
    c.write_json(path, new, sort_keys=False)
    r.step.warn(f"Updated {path} with the setup values of this run. Commit it through a PR/MR: "
                "npx projen branch chore/setup-values && npx projen push")


# ---- build ---------------------------------------------------------------------------------------

def toolchain():
    """The versions the project type pins (src/cre-demo-template-app.ts), for the bootstrap install."""
    with open(os.path.join(PKG, "src", "cre-demo-template-app.ts")) as f:
        src = f.read()

    def find(pattern):
        m = re.search(pattern, src)
        if not m:
            raise RuntimeError(f"no {pattern} in cre-demo-template-app.ts")
        return m.group(1)
    return {"projen": find(r"PROJEN_VERSION = '([^']+)'"), "aws-cdk-lib": find(r"cdkVersion: '([^']+)'"),
            "constructs": find(r"constructsVersion: '([^']+)'"), "typescript": find(r"typescriptVersion: '([^']+)'"), "ts-node": "*"}


def phase_build(r):
    step, demo = r.step, r.demo
    log_path = os.path.join(demo, ".cre", "new-project.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a")

    def sh(what, cmd):
        step.plan(what)
        log.write(f"\n$ {' '.join(cmd)}\n")
        log.flush()
        # Corepack would otherwise ask (on a closed stdin) before downloading Yarn.
        res = c.run(cmd, cwd=demo, env={"COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"})
        log.write(res.stdout + res.stderr)
        log.flush()
        if res.returncode == 0:
            return
        text = res.stdout + res.stderr
        tail = "\n".join(text.strip().splitlines()[-25:])
        # npm (E401) and Yarn Berry (YN0041, "Response code 401") word an expired registry token differently.
        if re.search(r"E401|Invalid authentication|YN0041|Response code 401|401 Unauthorized", text):
            step.die(c.CREDENTIALS, f"{what}: the npm registry refused the token (E401). On an Amazon machine ~/.npmrc points at "
                     "CodeArtifact, whose token lasts 22 hours: run  harmony npm  (it needs your Midway session), then re-run.\n" + tail)
        step.die(c.FAILED, f"{what} failed (exit {res.returncode}); full log: {log_path}\n{tail}")

    # A fresh scaffold has no package.json, so a bare `npx projen` would do nothing: seed the
    # bootstrap toolchain, run .projenrc.ts once with ts-node in CommonJS (ESM dir imports such as
    # projen/lib/github throw ERR_UNSUPPORTED_DIR_IMPORT), and projen takes over from there.
    try:
        pins = toolchain()
    except (OSError, RuntimeError) as e:
        step.die(c.FAILED, f"Reading the toolchain pins failed: {e}")
    with open(os.path.join(demo, "package.json"), "w") as f:
        json.dump({"name": "scaffold-bootstrap", "version": "0.0.0", "private": True, "devDependencies": pins}, f, indent=2)
    try:
        sh("npm install (bootstrap toolchain)", ["npm", "install", "--no-audit", "--no-fund"])
        sh("projen bootstrap (.projenrc.ts via ts-node)",
           ["npx", "ts-node", "--compiler-options", '{"module":"CommonJS","esModuleInterop":true}', ".projenrc.ts"])
        sh("yarn install", ["yarn", "install"])
        sh("npx projen", ["npx", "projen"])
        sh("yarn build (synth, tests, lint, package)", ["yarn", "build"])
    finally:
        log.close()
    # The green gate beyond the build's own checks.
    plan_file = os.path.join(demo, "cre-stacks.json")
    try:
        with open(plan_file) as f:
            json.load(f)
    except (OSError, ValueError) as e:
        step.die(c.FAILED, f"The build left no valid cre-stacks.json ({e}).")
    ci = os.path.join(demo, ".gitlab-ci.yml" if r.a["provider"] == "gitlab" else ".github/workflows/deploy.yml")
    if not os.path.isfile(ci) or r.a["awsRegion"] not in open(ci).read():
        step.die(c.FAILED, f"{ci} doesn't name the region {r.a['awsRegion']}.")
    if not os.path.isfile(os.path.join(demo, "dist", "content.zip")):
        step.die(c.FAILED, "The build produced no dist/content.zip.")
    step.ok("Build green: synth, tests, lint and packaging passed.")


def phase_push(r):
    if r.a["provider"] == "github":
        r.setup_step("repo_github", "repo-github", ["finalize", "--repo", r.repo, "--dir", r.demo], "push")
    else:
        r.setup_step("repo_gitlab", "repo-gitlab", ["finalize", "--namespace", r.a["gitlab"]["namespace"], "--project", r.a["name"],
                                                     "--dir", r.demo], "push")


PHASE_FNS = {"check": phase_check, "repo-create": phase_repo_create, "github-app": phase_github_app, "aws": phase_aws,
             "dns": phase_dns, "repo-config": phase_repo_config, "scaffold": phase_scaffold, "build": phase_build, "push": phase_push}
ONCE = ("scaffold", "build")   # skipped on a re-run once they succeeded (the rest is cheap and idempotent)


def main(step, argv):
    p = c.Parser(step, "new_project.py")
    p.add_argument("--answers", required=True)
    p.add_argument("--wait", type=int, default=None)
    p.add_argument("--from", dest="from_phase", choices=ONCE, default=None)
    p.add_argument("--yes", action="store_true")
    args = p.parse_args(argv)
    try:
        with open(args.answers) as f:
            raw = f.read()
        d = json.loads(raw)
    except (OSError, ValueError) as e:
        step.die(c.REFUSED, f"Can't read the answers file {args.answers}: {e}")
    a = check_answers(step, d)
    scaffold_inputs = {k: d.get(k) for k in SCAFFOLD_KEYS}
    scaffold_inputs["gitlabNamespace"] = (a.get("gitlab") or {}).get("namespace")
    answers_hash = hashlib.sha256(json.dumps(scaffold_inputs, sort_keys=True).encode()).hexdigest()
    state = State(step, re.sub(r"\.json$", "", args.answers) + ".state.json", answers_hash)
    r = Run(step, a, state, args)
    if state.data.get("answersHash") != answers_hash and any(state.done(x) for x in ONCE):
        step.die(c.REFUSED, f"The answers the project was scaffolded from changed ({', '.join(SCAFFOLD_KEYS)}). Revert "
                 f"{args.answers}, or start over: move {r.demo} away and delete {state.path}.")
    if args.from_phase == "scaffold" and state.done("push"):
        step.die(c.REFUSED, f"{r.demo} was pushed already: re-scaffolding would replace its history. To pick up a template fix, run "
                 "npx projen update-template in the project.")
    if args.from_phase:
        state.forget(ONCE[ONCE.index(args.from_phase):])
    # The steps run in this process: none may pick up a cre-setup.json from the current directory.
    os.environ["CRE_SETUP_FILE"] = os.devnull
    scaffolded_before = state.done("scaffold")
    try:
        for phase in PHASES:
            if phase in ONCE and state.done(phase):
                step.info(f"== {phase}: done in an earlier run (--from {phase} repeats it)")
            else:
                step.info(f"== {phase}")
                PHASE_FNS[phase](r)
                if phase in ONCE or phase == "push":
                    state.mark(phase)
            if phase == "scaffold":
                link_project_token(r)   # also for a project scaffolded before the link existed
                if os.path.isdir(r.demo) and r.values.get("RUNNER_ROLE_ARN"):
                    # The same values CI gets, for the local `npx projen deploy` (#31).
                    c.record_deploy_env(r.demo, {k: val for k, val, _ in deploy_variables(r)})
        if scaffolded_before:
            refresh_record(r)
    finally:
        r.report()
    step.out("DEMO_DIR", r.demo)
    step.out("REPO_WEB_URL", r.values.get("REPO_WEB_URL", ""))
    if step.needs:
        step.warn(f"Done except: {' '.join(step.needs)}. Do what the messages above say, then re-run: {' '.join(sys.argv)}")
    else:
        step.ok(f"{a['name']} is set up, built and pushed: {r.values.get('REPO_WEB_URL', '')}. Next: /pdd in {r.demo}.")


if __name__ == "__main__":
    c.cli(NAME, main)
