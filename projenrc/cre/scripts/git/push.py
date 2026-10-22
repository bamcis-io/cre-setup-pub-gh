#!/usr/bin/env python3
"""npx projen push --message "<conventional commit>" [--title "<MR/PR title>"] [--branch <new>]
                  [--files "<paths>"] [--allow-secret-path <path>]...

preflight → (new branch when needed) → validate → stage → commit → sync with the branch's own
remote (rebase, then validate again) → conflict check → re-check merged + auth → push → MR/PR open
and auto-merge armed.

Exit codes: 0 done | 10 refused, nothing changed | 20 validation/hook failed | 30 conflict |
            40 incomplete (committed, branched or pushed, then stopped): re-run resumes.
Last stdout line: RESULT code=<n> state=<STATE> branch=<b> sha=<sha> mr=<url|none>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cregit as g  # noqa: E402
from cregit import CONFLICT, INCOMPLETE, REFUSED, Die  # noqa: E402


def bootstrap(t):
    cur = g.gout("branch", "--show-current")
    if cur != "main":
        raise Die(REFUSED, "BOOTSTRAP", f"origin is empty. The first push must come from a local 'main' (you are on '{cur or 'detached HEAD'}').")
    g.validate(t)
    g.stage(t)
    g.commit(t)
    if not g.head():
        raise Die(REFUSED, "BOOTSTRAP", "Nothing to push: main has no commits.")
    t.provider.auth_check()
    if g.git_streamed("push", "-u", "origin", "main") != 0:
        raise Die(REFUSED, "PUSH_FAILED", "git push -u origin main failed; the commit is kept locally. Re-run to retry.")
    g.record_pushed("main", g.head())
    t.state, t.branch = "BOOTSTRAPPED", "main"
    g.ok("Pushed the first commit directly to main (no MR/PR for the bootstrap commit).")


def body(t, argv):
    g.preamble(t, argv)
    a = t.args
    g.check_message_and_title(a)
    t.provider.auth_check()
    g.fetch()
    t.default = g.default_branch()
    g.classify(t)
    if t.cls not in ("ON_DEFAULT", "MERGED", "CLOSED"):   # resolve_branch says it for those
        g.info(t.note)
    if t.cls == "BOOTSTRAP":
        return bootstrap(t)

    g.resolve_branch(t, a.branch)
    branch = t.branch

    g.validate(t)
    g.stage(t)
    committed = g.commit(t)

    # ---- sync with the branch's own remote (a teammate's push, projen's self-mutation commit)
    present, rsha = g.remote_branch(branch)
    if present:
        if not g.succeeds("cat-file", "-e", f"{rsha}^{{commit}}") and \
                not g.succeeds("fetch", "-q", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"):
            raise Die(REFUSED, "ORIGIN_UNREACHABLE", f"Could not fetch origin/{branch}.")
        if not g.succeeds("merge-base", "--is-ancestor", rsha, "HEAD"):
            g.info(f"origin/{branch} has commits you don't have; rebasing onto it.")
            # --autostash: changes left out of --files would otherwise stop the rebase.
            if not g.succeeds("rebase", "-q", "--autostash", rsha):
                g.git("rebase", "--abort")
                raise Die(CONFLICT, "CONFLICT_WITH_OWN_REMOTE", f"Your commits conflict with new commits on origin/{branch}. The rebase was "
                          "aborted and your branch is as it was" + (" (including the commit just made, not pushed)" if committed else "")
                          + f". Resolve with: git rebase origin/{branch}, then re-run.")
            t.done.append(f"rebased onto origin/{branch}")
            # The rebased tree is new: build it before it is pushed.
            before = g.gout("status", "--porcelain")
            g.validate(t, why=f"the rebased tree (onto origin/{branch})")
            if g.gout("status", "--porcelain") != before:
                raise Die(INCOMPLETE, "REGENERATED_AFTER_REBASE", f"The build of the rebased tree changed files (generated files differ "
                          f"after origin/{branch}'s commits). Nothing was pushed. Re-run the same command to commit and push them.")

    # ---- conflicts with the default branch block auto-merge forever
    conflicts = g.conflicts_with_default(t)
    if conflicts:
        raise Die(CONFLICT, "CONFLICT_WITH_DEFAULT", f"{branch} conflicts with origin/{t.default}, so its MR/PR could never merge. Not pushed"
                  + ("; the commit is kept locally" if committed else "") + ". Conflicting files:\n" + g.indent(conflicts)
                  + f"\n   Resolve with: git merge origin/{t.default} (or rebase), then re-run.")

    # ---- anything to push?
    head_sha = g.head()
    if not present and not committed and not g.rev_list(f"refs/remotes/origin/{t.default}..HEAD"):
        raise Die(REFUSED, "NOTHING_TO_PUSH", f"Nothing to commit, and {branch} has no commits that origin/{t.default} lacks.")
    if present and rsha == head_sha:
        g.info(f"Nothing new: origin/{branch} already has {g.short(head_sha)}.")
        g.record_pushed(branch, head_sha)
        t.provider.ensure_mr(t, push_output=None, first=False)
        t.state = t.state_override or "UP_TO_DATE"
        return

    # ---- last checks right before the push. The MR may have auto-merged while this ran (build
    # time): pushing then would recreate a deleted branch and open a new MR of merged commits.
    # Fetched again: telling "deleted on merge" from "deleted unmerged" needs the current main.
    g.fetch()
    if g.was_pushed(branch) and g.branch_merged(t, branch)[0]:
        raise Die(REFUSED, "MERGED_DURING_RUN", f"{branch} merged while this was running ({t.merged_reason}). Nothing was pushed. Re-run with "
                  "--branch <new-name> and the new work moves to a new branch.")
    t.provider.auth_check()   # a Midway SSH certificate can expire during a long build

    output = t.provider.push(branch, "" if present else a.title)
    g.record_pushed(branch, head_sha)
    t.done.append(f"pushed {branch}")
    t.provider.ensure_mr(t, push_output=output, first=not present)
    t.state = "PUSHED"
    g.ok(f"Pushed {branch} → {t.default}; MR/PR open{f' ({t.mr})' if t.mr != 'none' else ''}.")


if __name__ == "__main__":
    g.run_task("push", body)
