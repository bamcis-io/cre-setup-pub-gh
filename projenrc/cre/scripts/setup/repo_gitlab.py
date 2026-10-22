#!/usr/bin/env python3
"""Create or adopt the GitLab project for a CRE demo, using Midway credentials.

  repo_gitlab.py prepare  --namespace <group/path> --project <name> [--var KEY=VALUE[:protected][:masked]]...
                          [--bindle <owning team bindle id>]   (default: the aws-cre bindle)
  repo_gitlab.py finalize --namespace <group/path> --project <name> --dir <demo-dir> [--message <msg>]

In a project (npx projen setup:repo), --namespace, --project and --dir default to cre-setup.json
and its directory.

prepare (before the scaffold exists):
  - Checks the Midway cookie and SSH certificate (exit 11 with the refresh command).
  - Gets an `api` token for the REST calls (sent with the Midway cookie): one minted for a day over
    the Midway-signed SSH session and revoked on exit, when GitLab allows it (it refuses Midway
    certificate users today); otherwise $GITLAB_TOKEN or ~/.gitlab/access_token. Exit 11 says how
    to create one.
  - Adopts the project if it already exists (made by hand in the UI), otherwise creates it private,
    without a README, through the GitLab Management Portal API with an owning team Bindle
    (gitlab.aws.dev refuses GitLab's own create-project API).
  - Upserts the CI/CD variables (`:protected` marks one Protected: it reaches only protected-branch
    pipelines). That alone doesn't keep the runner role from other branches, since any branch's CI
    file can name the role ARN itself: the runner role's trust (aws_account.py) also requires the
    vendor's `GitLab:Project:ProtectedBranch = Yes` tag.
  Prints REPO_SSH_URL, REPO_WEB_URL, PROJECT_ID.

finalize (after the scaffold is green, in its directory):
  - Sets origin to the SSH URL, commits the scaffold if it has no commit yet, and pushes main as the
    first commit. A remote holding only a UI-created README/LICENSE/.gitignore commit is merged
    (scaffold wins); real history stops with exit 10.
  - Then protects main (push: no one, merge: developers), requires squash, and deletes source
    branches on merge. Protection is applied after the first push so it can't reject it.
  - The weekly dependency upgrade: a project access token `cre-upgrade` (write_repository,
    Developer, 364 days; rotated by a finalize run once it has under 30 days left) stored as the
    masked, Protected variable CRE_UPGRADE_TOKEN, and a pipeline schedule (Mondays 06:00 UTC, main,
    CRE_UPGRADE=1) that runs the pipeline's `upgrade:main` job.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crelib as c  # noqa: E402

NAME = "repo-gitlab"
DEFAULT_BINDLE = "amzn1.bindle.resource.v4o66fxvd3zfcqvcmhxq"
PROTECT = {"name": "main", "push_access_level": 0, "merge_access_level": 30}


def parse(step, argv):
    p = c.Parser(step, "repo_gitlab.py")
    p.add_argument("mode", choices=["prepare", "finalize"])
    p.add_argument("--namespace", default="")
    p.add_argument("--project", default="")
    p.add_argument("--dir", default="")
    p.add_argument("--message", default="chore: initial scaffold")
    p.add_argument("--var", action="append", default=[])
    p.add_argument("--bindle", default=os.environ.get("CRE_GITLAB_BINDLE", DEFAULT_BINDLE))
    c.common_flags(p)
    a = p.parse_args(argv)
    step.yes, step.dry_run = a.yes, a.dry_run
    sf = c.SetupFile(step)
    sf.default(a, "namespace", "namespace")
    sf.default(a, "project", "project")
    if not a.dir and sf.dir:
        a.dir = sf.dir
    sf.report()
    a.setup_file = sf
    if not a.namespace or not a.project:
        step.die(c.REFUSED, "--namespace and --project are required.")
    a.vars = [c.parse_var(step, v) for v in a.var]
    a.full = f"{a.namespace}/{a.project}"
    a.ssh_url = f"{c.GITLAB_SSH}:{a.full}.git"
    return a


def project_id(step, gl, full):
    """The project's id, or "" when it doesn't exist."""
    status, body = gl.call("GET", f"/projects/{c.urlenc(full)}")
    if status == "200":
        return str(body.get("id", ""))
    if status == "404":
        return ""
    if status in ("401", "403"):
        step.die(c.CREDENTIALS, f"GitLab rejected the API call ({status}). Refresh Midway (mwinit -f -k ~/.ssh/id_ecdsa.pub); if that "
                 "doesn't fix it, the stored token is expired or lacks the 'api' scope. Re-run after fixing.")
    step.die(c.FAILED, f"GET /projects/{full} returned {status}: {body}")


def set_variable(step, gl, pid, key, value, protected, masked=False):
    """Create or update a project CI/CD variable."""
    label = f"{key}{' (protected)' if protected else ''}{' (masked)' if masked else ''}"
    if step.dry_run:
        step.plan(f"(dry run) set CI/CD variable {label}")
        return
    body = {"key": key, "value": value, "protected": protected, "masked": masked,
            "environment_scope": "*", "variable_type": "env_var"}
    status, _ = gl.call("GET", f"/projects/{pid}/variables/{key}")
    if status == "200":
        status, resp = gl.call("PUT", f"/projects/{pid}/variables/{key}", body)
    else:
        status, resp = gl.call("POST", f"/projects/{pid}/variables", body)
    if status not in ("200", "201"):
        # The response may echo the value: report only GitLab's message.
        msg = resp.get("message") if isinstance(resp, dict) else ""
        step.die(c.FAILED, f"Setting CI/CD variable {key} failed ({status}): {msg}")
    step.ok(f"CI/CD variable {label} set.")


# ---- the weekly dependency upgrade -----------------------------------------------------------
# The pipeline's `upgrade:main` job (gitlab-workflow.ts) runs in a scheduled pipeline and pushes the
# upgrade to a branch with push options that open an MR set to merge when its pipeline passes. A
# job's own CI_JOB_TOKEN can't push, so it uses a project access token: write_repository only, this
# project only, Developer (to push a branch and merge to main, which merge = Developers allows), a
# year at most (GitLab's limit). Rotated by any finalize run once it has less than ROTATE_DAYS left.

UPGRADE_TOKEN_NAME = "cre-upgrade"
UPGRADE_VARIABLE = "CRE_UPGRADE_TOKEN"
UPGRADE_SCHEDULE = "CRE weekly dependency upgrade"
UPGRADE_CRON = "0 6 * * 1"    # Mondays 06:00 UTC
TOKEN_DAYS, ROTATE_DAYS = 364, 30


def ensure_upgrade_token(step, gl, pid):
    import datetime
    today = datetime.date.today()
    status, tokens = gl.call("GET", f"/projects/{pid}/access_tokens?state=active&per_page=100")
    if status != "200" or not isinstance(tokens, list):
        step.die(c.FAILED, f"Listing project access tokens failed ({status}): {tokens}")
    ours = [t for t in tokens if t.get("name") == UPGRADE_TOKEN_NAME and t.get("active", True) and not t.get("revoked")]
    var_status, _ = gl.call("GET", f"/projects/{pid}/variables/{UPGRADE_VARIABLE}")

    def days_left(t):
        try:
            return (datetime.date.fromisoformat(t.get("expires_at") or "") - today).days
        except ValueError:
            return 0
    fresh = [t for t in ours if days_left(t) > ROTATE_DAYS]
    if fresh and var_status == "200":
        step.ok(f"The upgrade's project access token is set ({days_left(fresh[0])} days left).")
        return
    why = "none yet" if not ours else ("expiring soon" if not fresh else f"the {UPGRADE_VARIABLE} variable is missing")
    if step.dry_run:
        step.plan(f"(dry run) create project access token {UPGRADE_TOKEN_NAME} ({why}) and store it as {UPGRADE_VARIABLE}")
        return
    step.plan(f"create project access token {UPGRADE_TOKEN_NAME} (write_repository, Developer, {TOKEN_DAYS} days; {why})")
    status, made = gl.call("POST", f"/projects/{pid}/access_tokens", {
        "name": UPGRADE_TOKEN_NAME, "scopes": ["write_repository"], "access_level": 30,
        "expires_at": (today + datetime.timedelta(days=TOKEN_DAYS)).isoformat()})
    if status != "201" or not isinstance(made, dict) or not made.get("token"):
        msg = made.get("message") if isinstance(made, dict) else made
        step.die(c.FAILED, f"Creating the project access token failed ({status}): {msg}. The weekly upgrade can't push without it.")
    set_variable(step, gl, pid, UPGRADE_VARIABLE, made["token"], protected=True, masked=True)
    # The new token is stored: the ones it replaces go.
    for t in ours:
        gl.call("DELETE", f"/projects/{pid}/access_tokens/{t['id']}")
    step.ok(f"Upgrade token created (expires {made.get('expires_at')}); a finalize run within {ROTATE_DAYS} days of that rotates it.")
    step.out("UPGRADE_TOKEN_EXPIRES", made.get("expires_at", ""))


def ensure_upgrade_schedule(step, gl, pid):
    status, schedules = gl.call("GET", f"/projects/{pid}/pipeline_schedules?per_page=100")
    if status != "200" or not isinstance(schedules, list):
        step.die(c.FAILED, f"Listing pipeline schedules failed ({status}): {schedules}")
    if any(s.get("description") == UPGRADE_SCHEDULE for s in schedules):
        step.ok(f"Pipeline schedule '{UPGRADE_SCHEDULE}' exists.")
        return
    if step.dry_run:
        step.plan(f"(dry run) create pipeline schedule '{UPGRADE_SCHEDULE}' ({UPGRADE_CRON} UTC, main)")
        return
    step.plan(f"create pipeline schedule '{UPGRADE_SCHEDULE}' ({UPGRADE_CRON} UTC, main)")
    status, made = gl.call("POST", f"/projects/{pid}/pipeline_schedules", {
        "description": UPGRADE_SCHEDULE, "ref": "main", "cron": UPGRADE_CRON, "cron_timezone": "UTC", "active": True})
    if status != "201" or not isinstance(made, dict) or "id" not in made:
        step.die(c.FAILED, f"Creating the pipeline schedule failed ({status}): {made}")
    status, resp = gl.call("POST", f"/projects/{pid}/pipeline_schedules/{made['id']}/variables",
                           {"key": "CRE_UPGRADE", "value": "1", "variable_type": "env_var"})
    if status != "201":
        step.die(c.FAILED, f"Setting CRE_UPGRADE=1 on the pipeline schedule failed ({status}): {resp}")
    step.ok(f"Pipeline schedule '{UPGRADE_SCHEDULE}' created: the weekly upgrade runs Mondays 06:00 UTC.")
    step.out("UPGRADE_SCHEDULE_ID", made["id"])


def prepare(step, gl, a):
    pid = project_id(step, gl, a.full)
    if pid:
        step.ok(f"GitLab project {a.full} exists (id {pid}); adopting it.")
    else:
        status, ns = gl.call("GET", f"/namespaces/{c.urlenc(a.namespace)}")
        if status != "200" or not str(ns.get("id", "")).isdigit():
            step.die(c.REFUSED, f"GitLab namespace '{a.namespace}' not found or not visible to you ({status}).")
        # gitlab.aws.dev disables GitLab's own POST /projects ("use the Amazon custom API"): projects
        # are created through the Management Portal API, which records the owning team Bindle.
        if step.dry_run:
            step.plan(f"(dry run) create private project {a.full} (owning bindle {a.bindle})")
            pid = "dry-run"
        else:
            step.plan(f"create private project {a.full} (owning bindle {a.bindle})")
            status, raw = gl.manage("POST", "/api/public/gitlab/createproject", {
                "project_name": a.project, "project_path": a.project, "namespace_id": int(ns["id"]),
                "private": True, "init_with_readme": False, "parent_bindle_id": a.bindle})
            if status not in ("200", "201"):
                step.die(c.FAILED, f"Creating {a.full} via {c.MANAGE_API} failed ({status}): {raw[:500]}")
            # The portal's response shape isn't documented, so read the id back from GitLab.
            pid = project_id(step, gl, a.full)
            if not pid:
                step.die(c.FAILED, f"The portal reported success but GitLab has no project {a.full} yet: {raw[:500]}")
            step.ok(f"Created {a.full} (id {pid}), owned by bindle {a.bindle}.")
    for key, value, protected, masked in a.vars:
        set_variable(step, gl, pid, key, value, protected, masked)
    if a.vars and not step.dry_run:
        c.record_ci_variables(a.setup_file, [v[0] for v in a.vars], [v[0] for v in a.vars if v[3]])
        if a.setup_file.dir:
            c.record_deploy_env(a.setup_file.dir, {k: val for k, val, _, masked in a.vars if not masked})
    step.out("PROJECT_ID", pid)
    step.out("REPO_SSH_URL", a.ssh_url)
    step.out("REPO_WEB_URL", f"https://{c.GITLAB_HOST}/{a.full}")


def protect_main(step, gl, pid):
    status, body = gl.call("GET", f"/projects/{pid}/protected_branches/main")
    if status != "200":
        status, body = gl.call("POST", f"/projects/{pid}/protected_branches", PROTECT)
        if status == "201":
            step.ok("Protected main (push: no one, merge: developers).")
            return
        if status != "409":
            step.die(c.FAILED, f"Protecting main failed ({status}): {body}")
        # GitLab protects the default branch itself right after the first push, so the GET above can
        # race it: read the protection GitLab made and judge it below.
        status, body = gl.call("GET", f"/projects/{pid}/protected_branches/main")
        if status != "200":
            step.die(c.FAILED, f"Reading main's protection failed ({status}): {body}")
    levels = [str(x.get("access_level")) for x in body.get("push_access_levels", [])]
    if levels == ["0"]:
        step.ok("main is protected (push: no one).")
        return
    # GitLab's own protection after the first push lets Maintainers push. Every change reaches main
    # through an MR (it deploys when the MR merges), so replace it: push no one, merge developers.
    # Protected branches can't be edited in place on this tier: unprotect, reprotect.
    status, body = gl.call("DELETE", f"/projects/{pid}/protected_branches/main")
    if status not in ("204", "404"):
        step.die(c.FAILED, f"Unprotecting main to tighten it failed ({status}): {body}")
    status, body = gl.call("POST", f"/projects/{pid}/protected_branches", PROTECT)
    if status != "201":
        step.die(c.FAILED, f"Protecting main failed ({status}): {body}. main is UNPROTECTED; re-run finalize.")
    step.ok(f"Protected main (push: no one, merge: developers), replacing GitLab's default (levels {','.join(levels)}).")


def finalize(step, gl, a):
    if not a.dir or not os.path.isdir(os.path.join(a.dir, ".git")):
        step.die(c.REFUSED, "--dir must be the scaffolded demo's git repository.")
    pid = project_id(step, gl, a.full)
    if not pid:
        step.die(c.REFUSED, f"GitLab project {a.full} doesn't exist; run 'repo_gitlab.py prepare' first.")
    c.set_origin(step, a.dir, a.ssh_url)
    c.require_main(step, a.dir)
    c.bootstrap_push(step, a.dir, a.message)
    if step.dry_run:
        return
    protect_main(step, gl, pid)
    # Prevent outdated deployment jobs: an older pipeline's deploy job never runs after a newer one
    # (with the per-stack resource groups, stacks can't be left at an older commit).
    status, body = gl.call("PUT", f"/projects/{pid}", {"squash_option": "always", "remove_source_branch_after_merge": True,
                                                       "ci_forward_deployment_enabled": True})
    if status != "200":
        step.die(c.FAILED, f"Updating merge settings failed ({status}): {body}")
    step.ok("Squash required on merge; source branches deleted on merge; outdated deploy jobs are skipped.")
    # The weekly dependency upgrade: after protection, so the token's variable is Protected and
    # reaches only main's (scheduled) pipelines.
    ensure_upgrade_token(step, gl, pid)
    ensure_upgrade_schedule(step, gl, pid)
    step.out("REPO_WEB_URL", f"https://{c.GITLAB_HOST}/{a.full}")


def main(step, argv):
    a = parse(step, argv)
    c.require_tools(step, "git", "ssh", "curl")
    gl = c.GitLab(step)
    gl.require_midway()
    with gl:
        gl.get_token()
        (prepare if a.mode == "prepare" else finalize)(step, gl, a)


if __name__ == "__main__":
    c.cli(NAME, main)
