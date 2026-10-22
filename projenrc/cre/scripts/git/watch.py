#!/usr/bin/env python3
"""npx projen watch — follow one MR/PR from its checks through the merge to the deploy of exactly
its merge commit, and stop at the first thing that needs attention. Read-only: it never pushes,
merges, retries or starts anything.

  npx projen watch                      the MR/PR of the current branch (open, or the latest one)
  npx projen watch <number>             MR !<number> / PR #<number>
  npx projen watch --branch <branch>    the latest MR/PR from <branch> (e.g. cre/upgrade-main,
                                        github-actions/upgrade-main: the weekly upgrade)
  npx projen watch --sha <sha>          only the deploy of commit <sha> on main (a first push)
  [--until checks|merged|deployed]      where to stop (default deployed)
  [--timeout <s>]                       give up after this long (default 3600)
  [--interval <s>]                      poll every s seconds (default 15)
  [--start-timeout <s>]                 how long a pipeline may take to appear (default 300)

Stages, each keyed on IDs read from the provider, never guessed:
  1. checks    the pipeline/check runs of the MR/PR's current head commit;
  2. merged    the MR/PR merged, and the commit it put on main (its merge or squash commit);
  3. deployed  the pipeline (GitLab) / `deploy` workflow run (GitHub) for exactly that commit on main.

Prints `KEY=value` lines as it learns them (MR, HEAD_SHA, CHECKS_URL, MERGE_SHA, DEPLOY_URL) and a
progress line on stderr on every change. On a failed job it prints the job's name, URL and the
last lines of its log. The last stdout line is `RESULT code=<n> stage=<stage> mr=<n> sha=<sha>`.

Exit codes: 0 reached --until | 10 nothing to watch (no MR/PR, bad input) | 11 credentials |
20 the MR/PR was closed without merging | 30 a check or deploy job failed | 40 timed out, or the
pipeline never started (the stage says which).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "setup"))
import crelib as c  # noqa: E402

DONE, CLOSED, FAILED, TIMEOUT = 0, 20, 30, 40
STAGES = ("checks", "merged", "deployed")
LOG_LINES = 40


class Stop(Exception):
    def __init__(self, code, stage, msg):
        super().__init__(msg)
        self.code, self.stage, self.msg = code, stage, msg


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def out(key, value):
    print(f"{key}={value}", flush=True)


def sh(cmd, input=None):
    return subprocess.run(cmd, input=input, stdin=None if input is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, errors="replace")


# ---- providers: the same small interface over GitHub (gh) and GitLab (REST) ---------------------
#   find(number, branch) -> MR dict or None     {number, state: open|merged|closed, head, url, merge_sha}
#   refresh(mr) -> MR dict
#   checks(sha, mr) -> Pipeline or None          {id, url, status: pending|running|success|failed|canceled, jobs}
#   deploy(sha) -> Pipeline or None
#   log_tail(job) -> str
# Jobs: [{id, name, status, url}]; status success|failed|skipped|canceled|running|pending|manual.

def norm_status(s):
    s = (s or "").lower()
    if s in ("success", "passed", "neutral"):
        return "success"
    if s in ("failure", "failed", "timed_out", "action_required", "startup_failure"):
        return "failed"
    if s in ("cancelled", "canceled"):
        return "canceled"
    if s in ("skipped",):
        return "skipped"
    if s in ("manual",):
        return "manual"
    if s in ("in_progress", "running"):
        return "running"
    return "pending"


def pipeline_status(jobs, own=None):
    """A pipeline's status from its jobs (or the provider's own status when it has one)."""
    if own in ("success", "failed", "canceled"):
        return own
    sts = [j["status"] for j in jobs]
    if not sts:
        return own or "pending"
    if "failed" in sts:
        return "failed" if all(s not in ("running", "pending") for s in sts) else "running"
    if any(s in ("running", "pending") for s in sts):
        return "running"
    if "canceled" in sts:
        return "canceled"
    return "success"


class GitHub:
    name = "github"

    def __init__(self):
        r = sh(["gh", "repo", "view", "--json", "nameWithOwner,url", "--jq", '.nameWithOwner + " " + .url'])
        if r.returncode != 0:
            raise Stop(c.CREDENTIALS, "checks", f"gh can't read this repo ({c.last_line(r.stderr)}). Run: gh auth login")
        self.repo, self.web = r.stdout.split()

    def _api(self, path):
        r = sh(["gh", "api", path])
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None

    def _mr(self, d):
        state = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}.get(d.get("state"), "open")
        return {"number": d["number"], "state": state, "head": d.get("headRefOid", ""), "url": d.get("url", ""),
                "merge_sha": (d.get("mergeCommit") or {}).get("oid", "") if state == "merged" else "",
                "branch": d.get("headRefName", "")}

    FIELDS = "number,state,headRefOid,url,mergeCommit,headRefName"

    def find(self, number, branch):
        if number:
            r = sh(["gh", "pr", "view", str(number), "-R", self.repo, "--json", self.FIELDS])
            return self._mr(json.loads(r.stdout)) if r.returncode == 0 else None
        r = sh(["gh", "pr", "list", "-R", self.repo, "--head", branch, "--state", "all", "--limit", "1", "--json", self.FIELDS])
        items = json.loads(r.stdout or "[]") if r.returncode == 0 else []
        return self._mr(items[0]) if items else None

    def refresh(self, mr):
        return self.find(mr["number"], None) or mr

    def _run_jobs(self, run_id):
        d = self._api(f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100") or {}
        return [{"id": j["id"], "name": j["name"], "url": j.get("html_url", ""),
                 "status": norm_status(j.get("conclusion") if j.get("status") == "completed" else j.get("status"))}
                for j in d.get("jobs", [])]

    def checks(self, sha, mr):
        """Every workflow run of the PR's head commit (build, title lint, auto-approve, ...), as one pipeline."""
        d = self._api(f"repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100") or {}
        runs = [r for r in d.get("workflow_runs", []) if r.get("event") in ("pull_request", "pull_request_target")]
        if not runs:
            return None
        # One PR starts the same workflow once per event (opened, each label added): a workflow
        # counts once, as its successful run if one succeeded (the others are redundant, and may
        # fail only because that one already did the work), else its latest run.
        by_name = {}
        for r in sorted(runs, key=lambda x: x["id"]):
            prev = by_name.get(r["name"])
            if prev is None or norm_status(prev.get("conclusion")) != "success":
                by_name[r["name"]] = r
        runs = list(by_name.values())
        jobs = []
        for r in runs:
            for j in self._run_jobs(r["id"]):
                j["name"] = f"{r['name']} / {j['name']}"
                jobs.append(j)
        return {"id": ",".join(str(r["id"]) for r in runs), "url": f"{mr['url']}/checks", "jobs": jobs,
                "status": pipeline_status(jobs)}

    def deploy(self, sha):
        d = self._api(f"repos/{self.repo}/actions/runs?head_sha={sha}&event=push&per_page=100") or {}
        runs = [r for r in d.get("workflow_runs", []) if r.get("name") == "deploy" and r.get("head_branch") == "main"]
        if not runs:
            return None
        r = sorted(runs, key=lambda x: x["id"])[-1]
        jobs = self._run_jobs(r["id"])
        own = norm_status(r.get("conclusion")) if r.get("status") == "completed" else None
        return {"id": str(r["id"]), "url": r.get("html_url", ""), "jobs": jobs, "status": pipeline_status(jobs, own)}

    def log_tail(self, job):
        r = sh(["gh", "api", f"repos/{self.repo}/actions/jobs/{job['id']}/logs"])
        return tail(r.stdout)


class GitLab:
    name = "gitlab"

    def __init__(self, step):
        url = sh(["git", "config", "--get", "remote.origin.url"]).stdout.strip()
        m = re.search(r"gitlab\.aws\.dev[:/](.+?)(\.git)?$", url)
        if not m:
            raise Stop(c.REFUSED, "checks", f"origin ({url or 'none'}) isn't a gitlab.aws.dev project.")
        self.full = m.group(1)
        self.enc = quote(self.full, safe="")
        self.web = f"https://{c.GITLAB_HOST}/{self.full}"
        self.gl = c.GitLab(step)
        try:
            self.gl.require_midway()
            self.gl.__enter__()
            self.gl.get_token()
        except c.Exit as e:
            raise Stop(e.code, "checks", "GitLab credentials (see above).")

    def close(self):
        self.gl.__exit__(None, None, None)

    def _get(self, path):
        status, body = self.gl.call("GET", f"/projects/{self.enc}{path}")
        if status in ("401", "403"):
            raise Stop(c.CREDENTIALS, "checks", f"GitLab refused the API call ({status}): refresh Midway (mwinit -f -k ~/.ssh/id_ecdsa.pub) or the token.")
        return body if status == "200" else None

    def _mr(self, d):
        return {"number": d["iid"], "state": {"opened": "open", "merged": "merged", "closed": "closed"}.get(d["state"], "open"),
                "head": d.get("sha", ""), "url": d.get("web_url", ""), "branch": d.get("source_branch", ""),
                # With a merge commit (the default), main's pipeline runs for it; fast-forward: the squash commit.
                "merge_sha": (d.get("merge_commit_sha") or d.get("squash_commit_sha") or "") if d["state"] == "merged" else ""}

    def find(self, number, branch):
        if number:
            d = self._get(f"/merge_requests/{number}")
            return self._mr(d) if isinstance(d, dict) and "iid" in d else None
        # The branch as is: GitLab matches source_branch=cre%2Fupgrade-main against nothing (verified).
        d = self._get(f"/merge_requests?source_branch={quote(branch, safe='/')}&state=all&order_by=created_at&sort=desc&per_page=1")
        return self._mr(d[0]) if isinstance(d, list) and d else None

    def refresh(self, mr):
        return self.find(mr["number"], None) or mr

    def _pipeline(self, p):
        jobs = [{"id": j["id"], "name": j["name"], "url": j.get("web_url", ""), "status": norm_status(j.get("status"))}
                for j in (self._get(f"/pipelines/{p['id']}/jobs?per_page=100&include_retried=false") or [])]
        own = norm_status(p.get("status"))
        return {"id": str(p["id"]), "url": p.get("web_url", ""), "jobs": jobs,
                "status": own if own in ("success", "failed", "canceled") else pipeline_status(jobs)}

    def checks(self, sha, mr):
        d = self._get(f"/merge_requests/{mr['number']}/pipelines")
        ps = [p for p in (d or []) if p.get("sha") == sha]
        return self._pipeline(sorted(ps, key=lambda x: x["id"])[-1]) if ps else None

    def deploy(self, sha):
        d = self._get(f"/pipelines?sha={sha}&ref=main&per_page=20")
        ps = [p for p in (d or []) if p.get("source") != "schedule"]
        return self._pipeline(sorted(ps, key=lambda x: x["id"])[-1]) if ps else None

    def log_tail(self, job):
        _, text = self.gl.call("GET", f"/projects/{self.enc}/jobs/{job['id']}/trace", raw=True)
        return tail(text)


def tail(text):
    text = re.sub(r"\x1b\[[0-9;]*[mK]", "", text or "")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-LOG_LINES:])


# ---- the watch ---------------------------------------------------------------------------------

class Watch:
    def __init__(self, provider, args, clock=time.time, sleep=time.sleep):
        self.p, self.a, self.clock, self.sleep = provider, args, clock, sleep
        self.deadline = clock() + args.timeout
        self.last = {}

    def wait(self, stage):
        if self.clock() >= self.deadline:
            raise Stop(TIMEOUT, stage, f"Timed out after {self.a.timeout}s at stage {stage}.")
        self.sleep(self.a.interval)

    def report(self, key, line):
        if self.last.get(key) != line:
            self.last[key] = line
            say(line)

    def fail_jobs(self, stage, pipe):
        bad = [j for j in pipe["jobs"] if j["status"] == "failed"]
        for j in bad:
            print(f"\n--- {j['name']} failed: {j['url']}\n{self.p.log_tail(j)}\n---", file=sys.stderr)
        names = ", ".join(j["name"] for j in bad) or pipe["status"]
        raise Stop(FAILED, stage, f"{stage}: {names} failed ({pipe['url']}).")

    def follow(self, stage, get, label):
        """Poll get() until its pipeline finishes; returns it. None for --start-timeout = it never started."""
        started = self.clock()
        while True:
            pipe = get()
            if pipe is None:
                if self.clock() - started > self.a.start_timeout:
                    raise Stop(TIMEOUT, stage, f"No {label} started within {self.a.start_timeout}s.")
                self.report(stage, f"{stage}: waiting for the {label} to start...")
                self.wait(stage)
                continue
            summary = ", ".join(f"{j['name']}={j['status']}" for j in pipe["jobs"]) or "no jobs yet"
            self.report(stage, f"{stage}: {pipe['status']} ({summary})")
            if pipe["status"] in ("success", "skipped"):
                return pipe
            if pipe["status"] in ("failed", "canceled"):
                self.fail_jobs(stage, pipe)
            self.wait(stage)

    def run(self):
        a = self.a
        if a.sha:
            out("MERGE_SHA", a.sha)
            pipe = self.follow("deployed", lambda: self.p.deploy(a.sha), f"deploy of {a.sha[:8]} on main")
            out("DEPLOY_URL", pipe["url"])
            return DONE, "deployed", "-", a.sha
        mr = self.p.find(a.number, a.branch)
        if not mr:
            what = f"#{a.number}" if a.number else f"from branch {a.branch}"
            raise Stop(c.REFUSED, "checks", f"No MR/PR {what}.")
        out("MR", mr["url"])
        # 1. checks of the head commit (the head can move: a new push restarts them)
        while True:
            mr = self.p.refresh(mr)
            out_once(self, "HEAD_SHA", mr["head"])
            if mr["state"] == "closed":
                raise Stop(CLOSED, "checks", f"{mr['url']} was closed without merging.")
            pipe = self.p.checks(mr["head"], mr)
            if pipe is None:
                if mr["state"] == "merged":
                    say("checks: none ran for the head commit (merged without them).")
                    break
                self.report("checks", "checks: waiting for the pipeline to start...")
                self.wait("checks")
                continue
            summary = ", ".join(f"{j['name']}={j['status']}" for j in pipe["jobs"]) or "no jobs yet"
            self.report("checks", f"checks: {pipe['status']} ({summary})")
            out_once(self, "CHECKS_URL", pipe["url"])
            if pipe["status"] == "success":
                break
            if mr["state"] == "merged":
                # Nothing required the checks (no branch protection), so the merge didn't wait; a
                # check failing now may only be the race (its branch deleted on merge). Go on to the deploy.
                say(f"checks: ⚠ merged before its checks passed ({summary}); nothing required them (branch protection).")
                break
            if pipe["status"] in ("failed", "canceled"):
                self.fail_jobs("checks", pipe)
            self.wait("checks")
        if a.until == "checks":
            return DONE, "checks", mr["number"], mr["head"]
        # 2. merged
        while mr["state"] != "merged":
            if mr["state"] == "closed":
                raise Stop(CLOSED, "merged", f"{mr['url']} was closed without merging.")
            self.report("merged", f"merged: waiting (state {mr['state']})...")
            self.wait("merged")
            mr = self.p.refresh(mr)
        for _ in range(10):
            if mr["merge_sha"]:
                break
            self.wait("merged")
            mr = self.p.refresh(mr)
        if not mr["merge_sha"]:
            raise Stop(TIMEOUT, "merged", f"{mr['url']} is merged, but the provider names no merge commit.")
        say(f"merged: {mr['url']} → {mr['merge_sha'][:8]} on main")
        out("MERGE_SHA", mr["merge_sha"])
        if a.until == "merged":
            return DONE, "merged", mr["number"], mr["merge_sha"]
        # 3. the deploy of exactly that commit
        pipe = self.follow("deployed", lambda: self.p.deploy(mr["merge_sha"]), f"deploy of {mr['merge_sha'][:8]} on main")
        out("DEPLOY_URL", pipe["url"])
        return DONE, "deployed", mr["number"], mr["merge_sha"]


def out_once(w, key, value):
    if value and w.last.get(f"out:{key}") != value:
        w.last[f"out:{key}"] = value
        out(key, value)


def parse(argv):
    p = argparse.ArgumentParser(prog="npx projen watch", allow_abbrev=False)
    p.add_argument("number", nargs="?", type=int)
    p.add_argument("--branch", default="")
    p.add_argument("--sha", default="")
    p.add_argument("--until", choices=STAGES, default="deployed")
    p.add_argument("--timeout", type=int, default=3600)
    p.add_argument("--interval", type=int, default=15)
    p.add_argument("--start-timeout", dest="start_timeout", type=int, default=300)
    a = p.parse_args(argv)
    if sum(bool(x) for x in (a.number, a.branch, a.sha)) > 1:
        p.error("give one of <number>, --branch, --sha")
    if not (a.number or a.branch or a.sha):
        a.branch = sh(["git", "branch", "--show-current"]).stdout.strip()
        if not a.branch or a.branch == "main":
            p.error("on main: give <number>, --branch <branch> or --sha <sha>")
    return a


def main(argv=None):
    a = parse(sys.argv[1:] if argv is None else argv)
    provider = os.environ.get("CRE_GIT_PROVIDER", "")
    p = None
    code, stage, number, sha = c.REFUSED, "checks", "-", "-"
    try:
        if provider == "github":
            p = GitHub()
        elif provider == "gitlab":
            p = GitLab(c.Step("watch", echo=False))
        else:
            raise Stop(c.REFUSED, "checks", "CRE_GIT_PROVIDER is not set (run it as npx projen watch).")
        code, stage, number, sha = Watch(p, a).run()
        say(f"✅ {stage}.")
    except Stop as e:
        print(f"❌ {e.msg}", file=sys.stderr)
        code, stage = e.code, e.stage
    except KeyboardInterrupt:
        code = TIMEOUT
    finally:
        if isinstance(p, GitLab):
            p.close()
    print(f"RESULT code={code} stage={stage} mr={number} sha={sha}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
