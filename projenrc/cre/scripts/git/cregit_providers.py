"""GitLab and GitHub for the git tasks (cregit.py). One interface:

  auth_check()                       credentials and origin reachable, else Die 10
  branch_closed(branch) -> bool      the branch's latest MR/PR was closed unmerged
  push(branch, title) -> output      git push (GitLab: with the MR push options); Die on failure
  ensure_mr(task, push_output, first)  the MR/PR open and auto-merge armed; Die 40 when it can't be

GitLab: the push options open the MR, assign it and arm auto-merge as the SSH-authenticated user,
with no token. When a GitLab API token is linked (.cre/gitlab-token, made at setup), the tasks also
read MR state through the API: a closed MR is detected, and an MR the push options didn't open (an
unknown assignee, a rejected option) or didn't arm is opened or armed. Without the link they warn
and work token-free, as before.

GitHub: the gh CLI, always against origin's repository (GH_REPO), never a guessed remote.
"""
import json
import os
import re
import sys
import time
from urllib.parse import quote

import crelib as c
from cregit import INCOMPLETE, REFUSED, Die, gout, info, ok, warn  # noqa: F401

PUSH_OPTION_ERROR = re.compile(r"(error encountered with push options?.*|push options? .*(invalid|not allowed|unknown).*)", re.I)


def run(cmd, input=None):
    return c.run(cmd, input=input)


def last(text):
    return c.last_line(text) if text.strip() else "no output"


# ---- GitLab --------------------------------------------------------------------------------------

class _Step:
    """crelib.GitLab's step interface, raising the git tasks' Die."""

    def info(self, m):
        info(m)

    def ok(self, m):
        ok(m)

    def warn(self, m):
        warn(m)

    def plan(self, m):
        info(m)

    def die(self, code, msg):
        raise Die(REFUSED, "GITLAB_CREDENTIALS" if code == c.CREDENTIALS else "GITLAB_API", msg)


class GitLab:
    name = "gitlab"

    def __init__(self, t):
        self.t = t
        # The configured URL: `git remote get-url` applies insteadOf rewrites.
        self.url = gout("config", "--get", "remote.origin.url")
        m = re.search(r"gitlab\.aws\.dev[:/](.+?)(\.git)?/?$", self.url)
        self.project = m.group(1) if m else ""
        self.web = f"https://{c.GITLAB_HOST}/{self.project}" if self.project else ""
        self._api = None
        self._api_checked = False

    # -- credentials

    def auth_check(self):
        if not os.environ.get("CRE_SKIP_MIDWAY_CHECK") and c.midway_expiry(c.midway_cookie()) <= time.time():
            raise Die(REFUSED, "AUTH_EXPIRED", "Midway cookie missing or expired. Run: mwinit -f -k ~/.ssh/id_ecdsa.pub")
        r = run(["git", "ls-remote", "--exit-code", "origin", "HEAD"])
        # Exit 2 = reachable but empty (a brand-new project); anything else is an auth/network failure.
        if r.returncode == 0 or run(["git", "ls-remote", "origin"]).returncode == 0:
            return
        if self.url.startswith(("https://", "http://")):
            raise Die(REFUSED, "AUTH_EXPIRED", f"Cannot reach origin ({self.url}). These tasks authenticate with the Midway-signed SSH "
                      f"certificate, so origin must be the SSH URL: git remote set-url origin git@ssh.{c.GITLAB_HOST}:{self.project or '<group>/<project>'}.git")
        raise Die(REFUSED, "AUTH_EXPIRED", "Cannot reach origin over SSH. Re-sign your key: mwinit -f -k ~/.ssh/id_ecdsa.pub, then check: "
                  f"ssh -T git@ssh.{c.GITLAB_HOST}")

    def api(self):
        """The REST client when a token is available, else None (token-free mode)."""
        if self._api_checked:
            return self._api
        self._api_checked = True
        path, _ = c.gitlab_token_file(os.getcwd())
        if not (os.environ.get("GITLAB_TOKEN") or path) or not self.project:
            return None
        gl = c.GitLab(_Step())
        gl.get_token(mint=False)   # a recorded but broken token file fails closed (Die 10)
        try:
            gl.__enter__()
        except OSError as e:
            raise Die(REFUSED, "AUTH_EXPIRED", f"Can't read the Midway cookie ({e}). Run: mwinit -f -k ~/.ssh/id_ecdsa.pub")
        self._api = GitLabApi(gl, self.project)
        return self._api

    def close(self):
        if self._api:
            self._api.gl.__exit__(None, None, None)

    # -- state

    def branch_closed(self, branch):
        api = self.api()
        if api is None:
            # Token-free, GitLab can't report MR state: a closed MR's branch looks ACTIVE, and the
            # next push opens a new MR for it.
            return False
        mr = api.latest_mr(branch, "all")
        return bool(mr) and mr.get("state") == "closed"

    # -- push

    def assignee(self):
        """merge_request.assign takes a username: GitLab's own SSH greeting names the identity the
        push authenticates as; the login name is only a last resort."""
        if "GL_PUSH_ASSIGNEE" in os.environ:
            return os.environ["GL_PUSH_ASSIGNEE"]
        url, host = self.url, ""
        if url.startswith("ssh://"):
            host = url[len("ssh://"):].split("/", 1)[0].split(":", 1)[0]
        elif "://" not in url and ":" in url:
            host = url.split(":", 1)[0]
        who = ""
        if host and not os.environ.get("CRE_SKIP_SSH_GREETING"):
            r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-T", host], input="")
            m = re.search(r"Welcome to GitLab, @([^!]+)!", r.stdout + r.stderr)
            who = m.group(1) if m else ""
        return who or os.environ.get("USER", "")

    def push(self, branch, title):
        """Push, opening/updating the MR. merge_request.create is a no-op when an open MR exists;
        merge_when_pipeline_succeeds re-arms auto-merge, which GitLab clears on every push to a
        branch with an open MR. The title is sent only on a branch's first push (title=""), so a
        later push never overwrites an MR title edited in the UI. `merge_when_pipeline_succeeds` is
        deprecated for `auto_merge` from GitLab 17.11, but still works there (verified live); the
        newer option is not sent, since an option a server doesn't know fails the MR creation."""
        opts = ["merge_request.create", f"merge_request.target={self.t.default}", "merge_request.merge_when_pipeline_succeeds",
                "merge_request.remove_source_branch",
                # Squash flag on the MR (GitLab 17.2+). setup:repo sets the project to squash always,
                # and GitLab then refuses to auto-merge an MR without it (seen live).
                "merge_request.squash"]
        if title:
            opts.append(f"merge_request.title={title}")
        who = self.assignee()
        if who:
            opts.append(f"merge_request.assign={who}")
        cmd = ["git", "push", "-u", "origin", branch]
        for o in opts:
            cmd += ["-o", o]
        r = run(cmd)
        text = r.stdout + r.stderr
        if text.strip():
            print(text.rstrip(), file=sys.stderr)
        if r.returncode != 0:
            raise Die(REFUSED, "PUSH_FAILED", f"git push failed; nothing reached origin. Re-run to retry.\n   {last(text)}")
        return text

    def ensure_mr(self, t, push_output=None, first=False):
        branch = t.branch
        if push_output:
            urls = re.findall(r"https?://\S+/-/merge_requests/\d+", push_output)
            if urls:
                t.mr = urls[-1]
        rejected = sorted({m.group(1).strip() for m in PUSH_OPTION_ERROR.finditer(push_output or "")})
        no_ci = not os.path.isfile(".gitlab-ci.yml") and not os.path.isdir(".gitlab")
        api = self.api()
        if api is None:
            new = f"{self.web}/-/merge_requests/new?merge_request%5Bsource_branch%5D={quote(branch, safe='')}" if self.web else "the project page"
            if rejected:
                raise Die(INCOMPLETE, "MR_OPTIONS_REJECTED", f"GitLab took the push but refused the MR push options ({'; '.join(rejected)}), so "
                          f"the MR may not be open or armed. Open it at {new}, or link your GitLab token file at {c.PROJECT_TOKEN_LINK} and re-run "
                          "(the task then opens and arms it).")
            if push_output is None:
                warn(f"Nothing new to push, and without a GitLab token ({c.PROJECT_TOKEN_LINK} isn't linked) the MR can't be checked. "
                     f"If it isn't open: {new}")
            if no_ci:
                warn("No .gitlab-ci.yml, so no pipeline runs and auto-merge has nothing to fire on. Merge the MR yourself.")
            return
        if rejected:
            warn(f"GitLab refused some MR push options ({'; '.join(rejected)}); checking the MR through the API.")
        mr = api.latest_mr(branch, "opened")
        if not mr and push_output is None:
            # Nothing pushed by this run: the MR may have finished meanwhile (auto-merge fired).
            done = api.latest_mr(branch, "all")
            if done and done.get("state") == "merged":
                t.mr = done.get("web_url") or t.mr
                ok(f"{t.mr} has already merged; nothing to do.")
                t.state_override = "MERGED"
                return
            if done and done.get("state") == "closed":
                raise Die(REFUSED, "NEEDS_BRANCH_CLOSED", f"{branch}'s MR was closed without merging; a closed branch is done. "
                          "Re-run with --branch <type>/<description>.")
        if not mr:
            mr = api.create_mr(branch, t.default, t.args.title)
            ok(f"Opened {mr.get('web_url', 'the MR')} through the API (the push options didn't).")
        t.mr = mr.get("web_url") or t.mr
        if no_ci:
            warn("No .gitlab-ci.yml, so no pipeline runs and auto-merge has nothing to fire on. Merge the MR yourself.")
            return
        if not armed(mr):
            api.arm(mr)
            ok("Auto-merge armed through the API.")


def armed(mr):
    return bool(mr.get("merge_when_pipeline_succeeds") or mr.get("auto_merge_enabled"))


class GitLabApi:
    """The MR calls, over crelib.GitLab (Midway cookie + token). Every failure fails closed."""

    def __init__(self, gl, project):
        self.gl = gl
        self.enc = quote(project, safe="")

    def _call(self, method, path, body=None, want=("200",)):
        status, data = self.gl.call(method, f"/projects/{self.enc}{path}" if not path.startswith("/user") else path, body)
        if status in ("401", "403"):
            path_file, where = c.gitlab_token_file(os.getcwd())
            raise Die(REFUSED, "GITLAB_CREDENTIALS", f"GitLab refused the token ({status}) from {where or '$GITLAB_TOKEN'}"
                      f"{': ' + path_file if path_file else ''}: it is expired or revoked, or the Midway session lapsed. "
                      "Run mwinit -f -k ~/.ssh/id_ecdsa.pub; if that doesn't fix it, replace the token in that file.\n" + c.token_help())
        if status not in want:
            raise Die(REFUSED, "GITLAB_API", f"GitLab API {method} {path} answered {status}: {str(data)[:300]}")
        return status, data

    def latest_mr(self, branch, state):
        # The branch unencoded except for what a URL can't hold: GitLab matches
        # source_branch=cre%2Fupgrade-main against nothing (verified).
        _, d = self._call("GET", f"/merge_requests?source_branch={quote(branch, safe='/')}&state={state}"
                                 "&order_by=created_at&sort=desc&per_page=1")
        return d[0] if isinstance(d, list) and d else None

    def create_mr(self, branch, target, title):
        try:
            _, me = self._call("GET", "/user")
            _, mr = self._call("POST", "/merge_requests", {
                "source_branch": branch, "target_branch": target, "title": title, "remove_source_branch": True,
                "squash": True, **({"assignee_id": me["id"]} if isinstance(me, dict) and me.get("id") else {})},
                want=("201", "200"))
        except Die as d:
            raise Die(INCOMPLETE, "MR_NOT_OPENED", f"Pushed {branch}, but no MR is open and opening one failed: {d.msg}")
        return mr

    def arm(self, mr):
        """Auto-merge, retried while GitLab isn't ready (405/406/422: the MR's pipeline not created
        yet, mergeability still being checked)."""
        tries = int(os.environ.get("CRE_GITLAB_ARM_TRIES") or 6)
        pause = float(os.environ.get("CRE_GITLAB_ARM_PAUSE") or 5)
        last_status, last_body = "", ""
        for i in range(tries):
            status, body = self.gl.call("PUT", f"/projects/{self.enc}/merge_requests/{mr['iid']}/merge",
                                        {"merge_when_pipeline_succeeds": True, "squash": True})
            if status == "200":
                return
            last_status, last_body = status, str(body)[:300]
            if status not in ("405", "406", "422"):
                break
            if i + 1 < tries:
                time.sleep(pause)
        raise Die(INCOMPLETE, "NOT_ARMED", f"The MR is open ({mr.get('web_url')}) but arming auto-merge failed ({last_status}: {last_body}). "
                  "Re-run to retry, or set it in the MR page.")


# ---- GitHub --------------------------------------------------------------------------------------

def merge_mode():
    """How a CRE GitHub project merges (cre-demo-template-app.ts githubMergeMode): "workflow" (a
    private repo: the build workflow merges after a passing build), "auto-merge" (public: GitHub
    auto-merge behind branch protection), or "" outside a CRE project (no cre-setup.json)."""
    try:
        with open("cre-setup.json") as f:
            v = json.load(f).get("githubVisibility")
    except (OSError, ValueError, AttributeError):
        return ""
    return "auto-merge" if v in ("public", "internal") else "workflow"


def github_repo(url):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(\.git)?/?$", url)
    return m.group(1) if m else ""


class GitHub:
    name = "github"

    def __init__(self, t):
        self.t = t
        # The configured URL: `git remote get-url` applies insteadOf rewrites.
        self.repo = github_repo(gout("config", "--get", "remote.origin.url"))
        if self.repo:
            # Every gh call (and gh api's {owner}/{repo}) targets origin's repo, never an `upstream`.
            os.environ["GH_REPO"] = self.repo

    def gh(self, *args):
        return run(["gh", *args])

    def auth_check(self):
        if self.gh("auth", "status").returncode != 0:
            raise Die(REFUSED, "AUTH_EXPIRED", "GitHub CLI is not authenticated. Run: gh auth login")
        if run(["git", "ls-remote", "origin"]).returncode != 0:
            raise Die(REFUSED, "AUTH_EXPIRED", "Cannot reach origin. Check: gh auth status, and the remote URL.")
        if not self.repo:
            warn(f"origin ({gout('remote', 'get-url', 'origin')}) isn't a github.com URL, so gh picks the repository itself.")

    def pr(self, branch):
        """(state, number) of the latest PR from <branch>: OPEN | MERGED | CLOSED | none. Fails CLOSED:
        a gh failure must never make a closed branch look active."""
        r = self.gh("pr", "list", "--head", branch, "--state", "all", "--limit", "1", "--json", "state",
                    "--jq", '.[0].state // "none"')
        if r.returncode != 0:
            raise Die(REFUSED, "GITHUB_UNREACHABLE", f"gh pr list failed, so {branch}'s PR state is unknown: {last(r.stderr)}")
        state = r.stdout.strip() or "none"
        num = ""
        if state == "OPEN":
            r = self.gh("pr", "list", "--head", branch, "--state", "open", "--limit", "1", "--json", "number", "--jq", ".[0].number")
            if r.returncode != 0:
                raise Die(REFUSED, "GITHUB_UNREACHABLE", f"gh pr list failed: {last(r.stderr)}")
            num = r.stdout.strip()
        return state, num

    def branch_closed(self, branch):
        return self.pr(branch)[0] == "CLOSED"

    def push(self, branch, title):
        r = run(["git", "push", "-u", "origin", branch])
        text = r.stdout + r.stderr
        if text.strip():
            print(text.rstrip(), file=sys.stderr)
        if r.returncode != 0:
            raise Die(REFUSED, "PUSH_FAILED", f"git push failed; nothing reached origin. Re-run to retry.\n   {last(text)}")
        return text

    def has_workflows(self):
        d = ".github/workflows"
        return os.path.isdir(d) and any(f.endswith((".yml", ".yaml")) for f in os.listdir(d))

    def required_checks(self, base):
        """True/False, or None when it can't be told."""
        r = self.gh("api", f"repos/{{owner}}/{{repo}}/branches/{base}/protection/required_status_checks",
                    "--jq", "(.checks // [] | length) + (.contexts // [] | length)")
        if r.returncode == 0:
            if (r.stdout.strip() or "0") != "0":
                return True
        elif "HTTP 404" not in r.stderr + r.stdout and "HTTP 403" not in r.stderr + r.stdout:
            return None
        r = self.gh("api", f"repos/{{owner}}/{{repo}}/rules/branches/{base}", "--jq", '[.[] | select(.type == "required_status_checks")] | length')
        if r.returncode != 0:
            return None
        return (r.stdout.strip() or "0") != "0"

    def ensure_mr(self, t, push_output=None, first=False):
        """Open (or reuse) the PR with its labels, then arm auto-merge, or merge directly when nothing
        gates it. Die 40 for anything left undone (labels included: without auto-approve the PR is
        never approved)."""
        problems = []
        state, num = self.pr(t.branch)
        labels = ["auto-approve", "auto-merge"]
        if state in ("MERGED", "CLOSED") and push_output is None:
            # Nothing was pushed by this run, and the PR finished meanwhile (it merged between
            # runs): never open a second PR for the same commits.
            r = self.gh("pr", "list", "--head", t.branch, "--state", "all", "--limit", "1", "--json", "url", "--jq", ".[0].url")
            t.mr = r.stdout.strip() or t.mr
            if state == "MERGED":
                ok(f"{t.mr} has already merged; nothing to do.")
                t.state_override = "MERGED"
                return
            raise Die(REFUSED, "NEEDS_BRANCH_CLOSED", f"{t.branch}'s PR was closed without merging; a closed branch is done. Re-run with --branch <type>/<description>.")
        if state != "OPEN":
            # Opened WITHOUT labels, then labelled in one edit: auto-approve approves on adding its
            # own label only, so the PR is approved once (opening it with both labels approved it
            # three times, one review email each).
            for label in labels:
                self.gh("label", "create", label, "--description", "merge once checks pass")
            r = self.gh("pr", "create", "--base", t.default, "--head", t.branch, "--title", t.args.title, "--body", "", "--assignee", "@me")
            if r.returncode != 0:
                raise Die(INCOMPLETE, "NOT_ARMED", f"Pushed {t.branch}, but gh pr create failed: {last(r.stderr)}. Re-run to retry.")
            m = re.findall(r"pull/(\d+)", r.stdout + r.stderr)
            num = m[-1] if m else ""
        if not num:
            raise Die(INCOMPLETE, "NOT_ARMED", "No PR number from gh. Re-run to retry.")
        url = self.gh("pr", "view", num, "--json", "url", "--jq", ".url").stdout.strip()
        t.mr = url or f"#{num}"
        # An existing PR keeps its title (--title is used only when the PR is created).
        r = self.gh("pr", "edit", num, "--add-label", ",".join(labels))
        if r.returncode != 0:
            problems.append(f"adding the auto-approve/auto-merge labels failed ({last(r.stderr)}), so it is neither approved nor merged; "
                            "a re-run adds them")
        mode = merge_mode()
        if mode == "workflow":
            # A private repo: the build workflow's merge job merges the PR once its build passes
            # (GitHub's auto-merge would merge at once: a free plan can't make it wait). Merge here
            # only when that build already passed for this exact commit (a re-run after the labels
            # were missing).
            head_sha = gout("rev-parse", "HEAD")
            if not problems and self.build_passed(head_sha):
                r = self.gh("pr", "merge", num, "--squash", "--match-head-commit", head_sha)
                if r.returncode != 0:
                    problems.append(f"its build passed, but the merge failed: {last(r.stderr)}")
                else:
                    ok(f"{t.mr}: its build had passed; merged.")
            elif not problems:
                info(f"{t.mr} merges when its build passes (the build workflow merges it).")
        else:
            gated = self.required_checks(t.default)
            if mode == "auto-merge" or gated is not False or self.has_workflows():
                if gated is False:
                    warn(f"{t.default} has no required status checks, so GitHub merges this PR as soon as it is mergeable, before its "
                         "workflows finish. Protect main with the build as a required check (setup:repo finalize does it on a public repo).")
                # No --delete-branch: setup sets delete_branch_on_merge, and gh's own deletion
                # would check out the default branch in this worktree.
                r = self.gh("pr", "merge", num, "--squash", "--auto")
                if r.returncode != 0:
                    problems.append(f"could not enable auto-merge (is it allowed on this repo?): {last(r.stderr)}")
            else:
                r = self.gh("pr", "merge", num, "--squash")
                if r.returncode != 0:
                    problems.append(f"the direct merge did not complete: {last(r.stderr)}")
        if problems:
            raise Die(INCOMPLETE, "NOT_ARMED", f"{t.mr} is open, but: {'; '.join(problems)}. Re-run to retry.")

    def build_passed(self, sha):
        r = self.gh("api", f"repos/{{owner}}/{{repo}}/commits/{sha}/check-runs?check_name=build", "--jq", ".check_runs[0].conclusion // \"\"")
        return r.returncode == 0 and r.stdout.strip() == "success"

    def close(self):
        pass
