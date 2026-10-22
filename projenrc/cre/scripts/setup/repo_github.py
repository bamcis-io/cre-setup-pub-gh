#!/usr/bin/env python3
"""Create or adopt the GitHub repository for a CRE demo, using the builder's saved `gh` login.

  repo_github.py prepare  --repo <owner>/<name> [--secret KEY=VALUE]...
                          [--app-slug <slug> [--app-id <id>] [--app-key-file <key.pem>]]
                          [--wait-install <seconds>]
  repo_github.py finalize --repo <owner>/<name> --dir <demo-dir> [--message <msg>]
  repo_github.py publish  [--yes]       (npx projen setup:github-public) a private repo made public

In a project (npx projen setup:repo), --repo and --app-slug default to cre-setup.json and --dir to
its directory.

prepare: checks `gh auth status` (exit 11 → `gh auth login`), adopts the repo if it exists or
  creates it private (an existing public or internal repo is adopted with a warning), and sets the
  Actions secrets (values piped to gh on stdin, never on its command line). With --app-slug: the
  builder's GitHub App (github_app.py creates it as aws-cre-automation-<alias>; its ID is looked up
  from the slug, its key defaults to ~/.cre/<slug>.pem), projen's only credential (gotcha 3). Its
  key is checked with GitHub (a JWT `GET /app`), PROJEN_APP_ID and PROJEN_APP_PRIVATE_KEY are
  stored, then its installation is checked:
    - installed on the repo with Contents, Pull requests and Workflows write: done;
    - not installed: installing is a browser step (GitHub has no API a CLI login may use; adding
      the repo to an existing installation is tried once and is refused in practice). The page
      opens when there is a GUI browser, its URL is printed as APP_INSTALL_URL, and the run ends 13
      with NEEDS=APP_INSTALL. --wait-install N first re-checks (as the App) every 10 s for up to N
      seconds (default: 600 when the page opened in a browser on this machine, 0 when it didn't).
    - installed with fewer permissions: NEEDS=APP_PERMISSIONS (accept them on the install page).
finalize: sets origin, commits and pushes main as the first commit (an initialize-with-README
  commit is merged, scaffold wins; real history stops), then enables auto-merge and squash merges,
  deletes branches on merge, lets Actions approve PRs, and protects main (the `build` check and one
  approval). A step it can't do prints NEEDS=<what> and the manual fix (exit 13): a free plan can't
  protect a private repo (BRANCH_PROTECTION); no App secrets yet (PROJEN_CREDENTIALS).
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

NAME = "repo-github"
PROTECTION = {"required_status_checks": {"strict": False, "contexts": ["build"]}, "enforce_admins": True,
              "required_pull_request_reviews": {"required_approving_review_count": 1}, "restrictions": None,
              "allow_force_pushes": False, "allow_deletions": False}


def parse(step, argv):
    p = c.Parser(step, "repo_github.py")
    p.add_argument("mode", choices=["prepare", "finalize", "publish"])
    p.add_argument("--repo", default="")
    p.add_argument("--dir", default="")
    p.add_argument("--message", default="chore: initial scaffold")
    p.add_argument("--secret", action="append", default=[])
    p.add_argument("--app-slug", dest="app_slug", default="")
    p.add_argument("--app-id", dest="app_id", default="")
    p.add_argument("--app-key-file", dest="app_key_file", default="")
    p.add_argument("--wait-install", dest="wait_install", type=int, default=None)
    c.common_flags(p)
    a = p.parse_args(argv)
    step.yes, step.dry_run = a.yes, a.dry_run
    sf = c.SetupFile(step)
    sf.default(a, "repo", "githubRepo")
    if a.mode == "prepare":
        sf.default(a, "app_slug", "appSlug")
    if not a.dir and sf.dir:
        a.dir = sf.dir
    sf.report()
    a.setup_file = sf
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a.repo or ""):
        step.die(c.REFUSED, "--repo must be <owner>/<name>.")
    a.owner = a.repo.split("/")[0]
    if a.wait_install is not None and a.wait_install < 0:
        step.die(c.REFUSED, "--wait-install takes a number of seconds.")
    a.secrets = [c.parse_secret(step, s) for s in a.secret]
    return a


def set_secret(step, repo, name, value):
    """On stdin, so the value never reaches argv."""
    if step.dry_run:
        step.plan(f"(dry run) set Actions secret {name}")
        return
    step.plan(f"set Actions secret {name}")
    r = c.gh("secret", "set", name, "-R", repo, input=value)
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"gh secret set {name}", r.stderr or r.stdout, repo.split("/")[0])


def git_url(repo):
    """origin's URL in the protocol gh is configured for (default https)."""
    proto = c.gh("config", "get", "git_protocol", "-h", "github.com").stdout.strip()
    return f"git@github.com:{repo}.git" if proto == "ssh" else f"https://github.com/{repo}.git"


def visibility(step, a):
    """PRIVATE / PUBLIC / INTERNAL, or "" when the repo doesn't exist."""
    r = c.gh("repo", "view", a.repo, "--json", "visibility", "--jq", ".visibility")
    if r.returncode == 0:
        return r.stdout.strip()
    text = r.stderr + r.stdout
    if "Could not resolve to a Repository" in text or "HTTP 404" in text:
        return ""
    c.gh_fail(step, c.FAILED, f"Reading {a.repo}", text, a.owner)


def install_page(owner, installation_id):
    """The installation's settings page (an organization's, or the user's own)."""
    if c.gh("api", f"orgs/{owner}").returncode == 0:
        return f"https://github.com/organizations/{owner}/settings/installations/{installation_id}"
    return f"https://github.com/settings/installations/{installation_id}"


class Install:
    """The App's installation as it covers this repo: state installed | owner | none."""

    def __init__(self, app, repo, owner):
        self.app, self.repo, self.owner = app, repo, owner
        self.state, self.id, self.missing = "none", "", []

    def check(self):
        self.state, self.id, self.missing = "none", "", []
        status, body = self.app.get(f"/repos/{self.repo}/installation")
        if status == "200":
            self.state, self.id = "installed", str(body.get("id", ""))
            self.missing = c.missing_permissions(body.get("permissions"))
            return self
        if status != "404":
            self.app.fail(status, f"/repos/{self.repo}/installation", body)
        for path in (f"/orgs/{self.owner}/installation", f"/users/{self.owner}/installation"):
            status, body = self.app.get(path)
            if status == "200":
                self.state, self.id = "owner", str(body.get("id", ""))
                break
        return self


def app_id_from_slug(step, slug):
    """The ID from the slug (public Apps answer without credentials). gh prints an error body on
    stdout on a 404, so only a number counts."""
    r = c.gh("api", f"apps/{slug}", "--jq", ".id")
    v = r.stdout.strip()
    if r.returncode != 0 or not v.isdigit():
        step.die(c.REFUSED, f"No GitHub App '{slug}' found (gh api apps/{slug}). Create it first: github_app.py "
                 "(npx projen setup:github-app in a project; new_project.py runs it).")
    return v


def setup_app(step, a):
    """projen's own credential (gotcha 3): its workflows push (self-mutation), open PRs (dependency
    upgrades) and enable auto-merge with it. The workflow's GITHUB_TOKEN can't: what it pushes,
    opens or merges starts no workflow. The builder's GitHub App is the only credential: never a
    personal access token, never the builder's gh token."""
    if not a.app_slug:
        step.die(c.REFUSED, "--app-slug is required with --app-id / --app-key-file.")
    if a.app_id and not a.app_id.isdigit():
        step.die(c.REFUSED, "--app-id must be the App's numeric ID (its settings page, 'App ID').")
    app_id = a.app_id or app_id_from_slug(step, a.app_slug)
    key = a.app_key_file or os.path.join(os.environ.get("CRE_KEY_DIR") or os.path.expanduser("~/.cre"), f"{a.app_slug}.pem")
    if not os.path.isfile(key) or not os.access(key, os.R_OK) or os.path.getsize(key) == 0:
        step.die(c.REFUSED, f"No readable private key at {key}. Run github_app.py (npx projen setup:github-app in a project; it says "
                 "how to make one), or pass --app-key-file.")
    app = c.App(step, a.app_slug, app_id, key)
    app.check_key()
    # Secrets first: they don't depend on the installation, and the scaffold can go on meanwhile.
    set_secret(step, a.repo, "PROJEN_APP_ID", app_id)
    with open(key) as f:
        set_secret(step, a.repo, "PROJEN_APP_PRIVATE_KEY", f.read())
    a.setup_file.set("appSlug", a.app_slug)
    step.out("APP_SLUG", a.app_slug)

    inst = Install(app, a.repo, a.owner).check()
    if inst.state == "owner" and not step.dry_run:
        # Installed on the owner, not on this repo. Adding the repo through the API with the gh login
        # is refused even for an org owner (verified Oct 2026: 403 "You do not have permission to
        # modify this app"); tried once, in case GitHub allows it later.
        r = c.gh("api", f"repos/{a.repo}", "--jq", ".id")
        repo_id = r.stdout.strip()
        if r.returncode == 0 and repo_id.isdigit() and \
                c.gh("api", "-X", "PUT", f"/user/installations/{inst.id}/repositories/{repo_id}").returncode == 0:
            added_to = inst.id
            if inst.check().state == "installed":
                step.ok(f"Added {a.repo} to GitHub App {a.app_slug}'s installation {added_to} on {a.owner}.")
    if inst.state != "installed":
        if inst.state == "owner":
            page = install_page(a.owner, inst.id)
            how = (f"Repository access → Only select repositories → add {a.repo} → Save. (If you can't save there, {a.owner} "
                   "lets only owners change App access: ask one.)")
        else:
            page = f"{c.github_web()}/apps/{a.app_slug}/installations/new"
            how = f"choose {a.owner} → Only select repositories → {a.repo} → Install."
        step.out("APP_INSTALL_URL", page)
        if step.dry_run:
            step.plan(f"(dry run) GitHub App {a.app_slug} isn't installed on {a.repo}: that would be a browser step ({page})")
        else:
            browser = c.open_url(page)
            opened = " (opened in your browser)" if browser else ""
            step.warn(f"Install GitHub App {a.app_slug} on {a.repo}, a browser step (GitHub has no API for it): {page}{opened} → {how}")
            # By default wait only when the page opened here (someone is there to click it).
            wait = a.wait_install if a.wait_install is not None else (600 if browser else 0)
            if wait > 0:
                poll = int(os.environ.get("CRE_APP_INSTALL_POLL", "10"))
                step.info(f"Waiting up to {wait}s for the install (checking as the App every {poll}s)...")
                deadline = time.time() + wait
                while time.time() < deadline:
                    time.sleep(poll)
                    if inst.check().state == "installed":
                        break
        if inst.state != "installed":
            step.info("Re-run this command once it's installed (--wait-install 90 waits for it). Everything else is set.")
            step.need("APP_INSTALL")
    if inst.state == "installed":
        step.ok(f"GitHub App {a.app_slug} is installed on {a.repo}.")
        if inst.missing:
            step.warn(f"Its installation lacks write access to: {' '.join(inst.missing)} (projen's workflows need Contents, Pull requests "
                      "and Workflows: write): the App was made by hand, or its new permissions weren't accepted. Give the App those "
                      f"permissions (github_app.py checks and says how), then accept the new permissions on the installation page, "
                      f"{install_page(a.owner, inst.id)}, and re-run.")
            step.need("APP_PERMISSIONS")


def prepare(step, a):
    vis = visibility(step, a)
    if vis == "PRIVATE":
        step.ok(f"GitHub repo {a.repo} exists (private); adopting it and completing its setup.")
    elif vis == "INTERNAL":
        step.warn(f"GitHub repo {a.repo} exists and is INTERNAL; adopting it. Every member of the enterprise can read its code, "
                  "cre-setup.json (account ID, role ARNs) and workflow logs. Make it private unless that is intended.")
    elif vis:
        # Made by hand, possibly public on purpose: a free plan protects main only on public repos.
        step.warn(f"GitHub repo {a.repo} exists and is NOT private; adopting it. Its code, cre-setup.json (account ID, role ARNs) "
                  "and workflow logs are public. Make it private unless that is intended.")
    elif step.dry_run:
        step.plan(f"(dry run) create private repo {a.repo}")
    else:
        step.plan(f"create private repo {a.repo}")
        r = c.gh("repo", "create", a.repo, "--private")
        if r.returncode != 0:
            out = r.stderr + r.stdout
            if "SAML" in out or "HTTP 401" in out:
                c.gh_fail(step, c.FAILED, f"Creating {a.repo}", out, a.owner)
            if re.search(r"HTTP 403|not have permission|correct permissions|Resource not accessible|is not a member", out):
                step.die(c.REFUSED, f"You may not create repositories in {a.owner} ({c.last_line(out)}). Ask an owner of {a.owner} to create "
                         f"{a.repo} (private, empty), then re-run: an existing repo is adopted.")
            step.die(c.FAILED, f"gh repo create {a.repo} failed: {c.last_line(out)}")
    # How PRs will merge (cre-setup.json githubVisibility → the project's merge mode).
    step.out("REPO_VISIBILITY", (vis or "PRIVATE").lower())
    # In a project: recorded where its merge mode is read from (a project made before the field
    # existed gets it here).
    if not step.dry_run:
        a.setup_file.set("githubVisibility", (vis or "PRIVATE").lower())
    for key, value in a.secrets:
        set_secret(step, a.repo, key, value)
    if a.secrets and not step.dry_run:
        # Every GitHub secret is masked in the logs.
        names = [k for k, _ in a.secrets]
        c.record_ci_variables(a.setup_file, names, names)
    if a.app_slug or a.app_id or a.app_key_file:
        setup_app(step, a)
    else:
        step.info("No --app-slug: PROJEN_APP_ID / PROJEN_APP_PRIVATE_KEY not changed (finalize checks they're set).")
    step.out("REPO_SSH_URL", f"git@github.com:{a.repo}.git")
    step.out("REPO_GIT_URL", git_url(a.repo))
    step.out("REPO_WEB_URL", f"https://github.com/{a.repo}")


def finalize(step, a):
    if not a.dir or not os.path.isdir(os.path.join(a.dir, ".git")):
        step.die(c.REFUSED, "--dir must be the scaffolded demo's git repository.")
    if not visibility(step, a):
        step.die(c.REFUSED, f"GitHub repo {a.repo} doesn't exist; run 'repo_github.py prepare' first.")
    # The protocol the builder's gh login uses for git (`gh auth login` asks): SSH needs a key on
    # their GitHub account, HTTPS a credential helper. Either way, use what already works for them.
    c.set_origin(step, a.dir, git_url(a.repo))
    c.require_main(step, a.dir)
    c.bootstrap_push(step, a.dir, a.message)
    if step.dry_run:
        return
    r = c.gh("api", "-X", "PATCH", f"repos/{a.repo}", "-F", "allow_auto_merge=true", "-F", "allow_squash_merge=true",
             "-F", "delete_branch_on_merge=true")
    if r.returncode != 0:
        c.gh_fail(step, c.FAILED, f"Updating {a.repo} merge settings", r.stderr + r.stdout, a.owner)
    step.ok("Auto-merge and squash merges enabled; branches deleted on merge.")
    # projen's auto-approve approves with the workflow token, which GitHub allows only when the repo
    # lets Actions approve pull requests.
    r = c.gh("api", "-X", "PUT", f"repos/{a.repo}/actions/permissions/workflow", "-f", "default_workflow_permissions=read",
             "-F", "can_approve_pull_request_reviews=true")
    if r.returncode == 0:
        step.ok("GitHub Actions may approve pull requests (projen's auto-approve).")
    else:
        step.warn(f"Could not let GitHub Actions approve pull requests on {a.repo} (an organization policy, or no admin rights). "
                  "auto-approve can't approve until it is allowed: Settings > Actions > General > Allow GitHub Actions to create and "
                  "approve pull requests.")
        step.need("ACTIONS_CAN_APPROVE")
    # Branch protection, GitLab's push = no one: every change reaches main through a PR that the
    # build passed and someone (auto-approve, for a trusted author) approved; the merge deploys.
    r = c.gh("api", "-X", "PUT", f"repos/{a.repo}/branches/main/protection", "--input", "-", input=json.dumps(PROTECTION))
    out = r.stderr + r.stdout
    if r.returncode == 0:
        step.ok("Protected main: changes only through a PR with a passing build check and an approval.")
    elif "Upgrade to GitHub Pro" in out:
        # GitHub Free protects branches only on public repos. A private CRE project doesn't rely on
        # it: its build workflow merges each PR only after the PR's build passes (merge mode
        # "workflow"). What protection would add is refusing direct pushes to main.
        step.info(f"GitHub can't protect main on {a.repo} (a free plan protects only public repos). PRs still merge only after "
                  "their build passes: the build workflow merges them. Direct pushes to main aren't blocked. When the repo goes "
                  "public: npx projen setup:github-public (it protects main and switches to GitHub auto-merge).")
    else:
        # Setup may lack the rights to change it, while an admin protected it already: check.
        g = c.gh("api", f"repos/{a.repo}/branches/main/protection", "--jq", ".required_status_checks.contexts[]")
        if g.returncode == 0 and "build" in g.stdout.split():
            step.ok(f"main is already protected with the build check (setup couldn't change it: {c.last_line(r.stderr)}).")
        else:
            step.warn(f"Could not protect main on {a.repo} ({c.last_line(r.stderr)}), and it isn't protected with the build check. Ask "
                      "a repo admin to protect main: Settings > Branches > require a pull request with one approval and the 'build' "
                      "status check.")
            step.need("BRANCH_PROTECTION")
    # projen's workflows need the App's secrets (prepare --app-slug sets them).
    names = [ln.split()[0] for ln in c.gh("secret", "list", "-R", a.repo).stdout.splitlines() if ln.strip()]
    if "PROJEN_APP_ID" in names and "PROJEN_APP_PRIVATE_KEY" in names:
        step.ok("PROJEN_APP_ID and PROJEN_APP_PRIVATE_KEY are set.")
    else:
        step.warn(f"projen's workflows have no credential: PROJEN_APP_ID / PROJEN_APP_PRIVATE_KEY aren't set on {a.repo}, so upgrade "
                  "PRs, self-mutation and auto-merge fail. Run github_app.py once (npx projen setup:github-app; it makes "
                  f"aws-cre-automation-<alias>), then: repo_github.py prepare --repo {a.repo} --app-slug aws-cre-automation-<alias>")
        step.need("PROJEN_CREDENTIALS")
    step.out("REPO_WEB_URL", f"https://github.com/{a.repo}")


# Credential formats looked for in the whole history before a repo goes public (the git tasks'
# list, cregit.py SECRET_CONTENT): once public, every commit ever pushed is readable.
HISTORY_SECRETS = (r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----|glpat-[A-Za-z0-9_-]{20}|"
                   r"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,}|xox[baprs]-[A-Za-z0-9-]{10,}|npm_[A-Za-z0-9]{36}|"
                   r"aws_secret_access_key *= *[A-Za-z0-9/+]{40}")


PUBLISH_BRANCH = "chore/public-repo-workflows"
# The git tasks (push.py, watch.py) beside setup/. CRE_GIT_SCRIPTS replaces them (the offline harness).
GIT_DIR = os.environ.get("CRE_GIT_SCRIPTS") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "git")


def publish(step, a):
    """A private repo made public, end to end, so the next PR already merges the public way:
      1. preflight: in the project, on main, nothing uncommitted (the switch is committed on its
         own), and no credential anywhere in the history;
      2. what becomes readable, then (only with --yes) the repo made public;
      3. main protected (the build check, one approval), githubVisibility=public recorded;
      4. `npx projen` regenerates the workflows (auto-queue replaces the build workflow's merge job),
         and the git tasks' push opens the PR for it (GitHub auto-merge, behind the new protection);
      5. it waits for that PR to merge and fast-forwards main, so the new workflows are on main
         (auto-approve and auto-queue run main's copy) before any PR with application code.
    Re-running carries on from wherever it stopped."""
    if not a.dir or not os.path.isfile(os.path.join(a.dir, "cre-setup.json")):
        step.die(c.REFUSED, "Run it in the project (npx projen setup:github-public): it records the change in cre-setup.json.")
    vis = visibility(step, a)
    if not vis:
        step.die(c.REFUSED, f"GitHub repo {a.repo} doesn't exist.")
    sf = a.setup_file
    git = lambda *x: c.run(["git", "-C", a.dir, *x])  # noqa: E731
    branch = git("branch", "--show-current").stdout.strip()
    dirty = [ln for ln in git("status", "--porcelain").stdout.splitlines() if ln[3:] != "cre-setup.json"]
    if branch != "main" or dirty:
        step.die(c.REFUSED, "Switching to public commits the regenerated workflows on their own, so start on main with nothing "
                 f"uncommitted (now: {branch or 'detached HEAD'}{', changes in ' + ', '.join(ln[3:] for ln in dirty[:5]) if dirty else ''}). "
                 "Push or stash your work, git switch main, then re-run.")
    if vis != "PRIVATE":
        step.ok(f"{a.repo} is already {vis.lower()}.")
    else:
        r = git("log", "--all", "-G", HISTORY_SECRETS, "--format=%h %s")
        if r.returncode != 0:
            step.die(c.FAILED, f"Couldn't scan the history: {c.last_line(r.stderr)}")
        hits = [ln for ln in r.stdout.splitlines() if re.match(r"^[0-9a-f]{7,} ", ln)]
        if hits:
            step.die(c.REFUSED, "The history has commits that look like they hold a credential; public, they'd be readable by anyone:\n"
                     + "\n".join("     " + h for h in hits[:20]) + "\n   Revoke those credentials and rewrite that history first (or keep the "
                     "repo private).")
        step.info(f"Making {a.repo} PUBLIC makes readable by anyone: all its code and history, cre-setup.json (AWS account "
                  f"{sf.data.get('accountId', '?')}, role ARNs), the workflow logs, issues and PRs. Secrets stay secret. The history "
                  "scan found no known credential format. It then protects main and pushes and merges the regenerated workflows.")
        if not step.confirm(f"Make {a.repo} public?"):
            if not step.dry_run:
                step.die(c.REFUSED, f"{a.repo} left private.")
            return
        r = c.gh("repo", "edit", a.repo, "--visibility", "public", "--accept-visibility-change-consequences")
        if r.returncode != 0:
            c.gh_fail(step, c.FAILED, f"Making {a.repo} public", r.stderr + r.stdout, a.owner)
        step.ok(f"{a.repo} is public.")
    if step.dry_run:
        return
    r = c.gh("api", "-X", "PUT", f"repos/{a.repo}/branches/main/protection", "--input", "-", input=json.dumps(PROTECTION))
    if r.returncode != 0:
        step.die(c.FAILED, f"Could not protect main ({c.last_line(r.stderr + r.stdout)}). The repo is public; until main is protected, "
                 "GitHub auto-merge wouldn't wait for the build, so nothing was switched. Fix it (admin rights?), then re-run.")
    step.ok("Protected main: changes only through a PR with a passing build check and an approval.")
    sf.set("githubVisibility", "public")

    # The regenerated workflows, committed and merged through the git tasks.
    env = {"CRE_GIT_PROVIDER": "github"}
    step.plan("npx projen (regenerate the workflows for GitHub auto-merge)")
    r = c.run(["npx", "projen"], cwd=a.dir)
    if r.returncode != 0:
        step.die(c.FAILED, f"npx projen failed: {c.last_line(r.stderr + r.stdout)}. The repo is public and main protected; fix it, then re-run.")
    if not git("status", "--porcelain").stdout.strip():
        step.ok("The workflows on main are already the public repo's.")
        return
    step.plan(f"push the regenerated workflows as a PR ({PUBLISH_BRANCH}) and merge it")
    r = c.run(["python3", os.path.join(GIT_DIR, "push.py"), "--message", "ci: merge through GitHub auto-merge now the repo is public",
               "--branch", PUBLISH_BRANCH, "--files", "cre-setup.json"], cwd=a.dir, env=env)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        step.die(c.FAILED, f"Pushing the regenerated workflows failed ({c.last_line(r.stdout)}). Fix what it says, then re-run.")
    branch = git("branch", "--show-current").stdout.strip()
    timeout = os.environ.get("CRE_PUBLISH_TIMEOUT", "2700")
    step.plan(f"wait for the PR from {branch} to merge (its build, then auto-merge; up to {timeout} s)")
    r = c.run(["python3", os.path.join(GIT_DIR, "watch.py"), "--branch", branch, "--until", "merged", "--timeout", timeout],
              cwd=a.dir, env=env)
    sys.stderr.write(r.stderr[-3000:])
    if r.returncode != 0:
        step.die(c.FAILED, f"The workflows PR didn't merge ({c.last_line(r.stdout)}). See it with npx projen watch --branch {branch}; "
                 "once it merges, nothing else is needed.")
    # Back to main, caught up with the merge (the PR's branch is deleted on merge).
    git("switch", "-q", "main")
    if git("fetch", "-q", "origin", "main").returncode == 0:
        git("merge", "-q", "--ff-only", "origin/main")
    step.ok(f"{a.repo} is public, main is protected, and the auto-merge workflows are on main: the next PR merges through "
            "GitHub auto-merge once its build passes and it is approved.")


def main(step, argv):
    a = parse(step, argv)
    c.require_tools(step, "gh", "git", "curl", "openssl")
    c.require_gh(step)
    {"prepare": prepare, "finalize": finalize, "publish": publish}[a.mode](step, a)


if __name__ == "__main__":
    c.cli(NAME, main)
