#!/usr/bin/env python3
"""npx projen ci:rerun — re-run the deploy of a commit on main, without a new commit.

  npx projen ci:rerun                 retry the failed jobs of the deploy of origin/main's head
  npx projen ci:rerun --sha <sha>     ... of that commit's deploy
  npx projen ci:rerun --all           run the whole deploy again (a redeploy of origin/main's head:
                                      e.g. after fixing a setting outside the repo)

Finds the deploy exactly as `watch` does (the GitLab pipeline / GitHub `deploy` run for that commit
on main) and asks the provider to retry it: GitLab retries the pipeline's failed jobs, or (--all)
starts a new main pipeline; GitHub re-runs the run's failed jobs (--failed), or all of them. Then
follow it with `npx projen watch --sha <sha>`. It never pushes or merges.

Exit codes: 0 started | 10 nothing to re-run (no deploy for that commit, still running, or it passed
and --all wasn't given) | 11 credentials | 20 the provider refused.
Last stdout line: RESULT code=<n> sha=<sha> url=<url|none>
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import watch as w  # noqa: E402

c = w.c


def git(*args):
    r = subprocess.run(["git", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def rerun(p, sha, all_jobs):
    pipe = p.deploy(sha)
    if not pipe:
        raise w.Stop(c.REFUSED, "deployed", f"No deploy pipeline/run for {sha[:9]} on main. A merge that changed nothing deployable, "
                     "or a commit that never reached main.")
    status = pipe["status"]
    if status in ("pending", "running"):
        raise w.Stop(c.REFUSED, "deployed", f"The deploy of {sha[:9]} is still {status} ({pipe['url']}): follow it with npx projen watch --sha {sha}.")
    if status == "success" and not all_jobs:
        raise w.Stop(c.REFUSED, "deployed", f"The deploy of {sha[:9]} passed ({pipe['url']}); nothing failed to retry. --all runs it again.")
    if p.name == "github":
        cmd = ["gh", "run", "rerun", pipe["id"], "-R", p.repo] + ([] if all_jobs else ["--failed"])
        r = w.sh(cmd)
        if r.returncode != 0:
            raise w.Stop(c.FAILED, "deployed", f"gh run rerun failed: {c.last_line(r.stderr)}")
        return pipe["url"]
    if all_jobs:
        head = git("rev-parse", "refs/remotes/origin/main")
        if sha != head:
            raise w.Stop(c.REFUSED, "deployed", f"--all starts a new pipeline for main's head ({head[:9]}), not {sha[:9]}. Without --all it "
                         "retries that commit's failed jobs.")
        status, d = p.gl.call("POST", f"/projects/{p.enc}/pipeline", {"ref": "main"})
    else:
        status, d = p.gl.call("POST", f"/projects/{p.enc}/pipelines/{pipe['id']}/retry")
    if status in ("401", "403"):
        raise w.Stop(c.CREDENTIALS, "deployed", f"GitLab refused ({status}): your role may not run main's pipeline, or the token/Midway lapsed.")
    if status not in ("200", "201"):
        raise w.Stop(c.FAILED, "deployed", f"GitLab refused the retry ({status}): {str(d)[:300]}")
    return (d or {}).get("web_url") or pipe["url"]


def main(argv=None):
    ap = argparse.ArgumentParser(prog="ci:rerun")
    ap.add_argument("--sha", default="")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    provider = os.environ.get("CRE_GIT_PROVIDER", "")
    p, code, url, sha = None, c.OK, "none", a.sha
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], stdin=subprocess.DEVNULL, capture_output=True)
        if not sha:
            sha = git("rev-parse", "refs/remotes/origin/main")
        else:
            sha = git("rev-parse", "--verify", f"{sha}^{{commit}}") or sha
        if not sha:
            raise w.Stop(c.REFUSED, "deployed", "No origin/main to re-run.")
        if provider == "github":
            p = w.GitHub()
        elif provider == "gitlab":
            p = w.GitLab(c.Step("ci", echo=False))
        else:
            raise w.Stop(c.REFUSED, "deployed", "CRE_GIT_PROVIDER is not set (run it as npx projen ci:rerun).")
        url = rerun(p, sha, a.all)
        w.say(f"✅ {'Re-running the deploy' if a.all else 'Retrying the failed deploy jobs'} of {sha[:9]}: {url}")
        w.say(f"   Follow it: npx projen watch --sha {sha}")
    except w.Stop as e:
        print(f"❌ {e.msg}", file=sys.stderr)
        code = e.code
    finally:
        if isinstance(p, w.GitLab):
            p.close()
    print(f"RESULT code={code} sha={sha or 'none'} url={url}", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
