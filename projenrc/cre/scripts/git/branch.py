#!/usr/bin/env python3
"""npx projen branch <name>

Start new work: fetch, create <name> from origin/<default> and bring along the uncommitted changes
(plus unpushed commits made on the default branch, or commits made after a branch merged), then
reset the local default branch to origin's when you were on it. From a detached HEAD the branch
starts at HEAD instead, so its commits stay. On conflict nothing changes and it exits 30.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cregit as g  # noqa: E402
from cregit import REFUSED, Die  # noqa: E402


def body(t, argv):
    g.preamble(t, argv)
    name = t.args.branch or (t.args.positional[0] if t.args.positional else "")
    g.fetch()
    t.default = g.default_branch()
    if not t.default:
        raise Die(REFUSED, "BOOTSTRAP", "origin has no branches yet. Make the first commit on main and push it directly (npx projen push does this).")
    g.validate_new_branch_name(t, name)
    g.classify(t)
    if t.cls == "MERGED_UNKNOWN_BASE":
        raise Die(REFUSED, "MERGED_UNKNOWN_BASE", f"{t.note} There is no record of what was pushed, so new commits can't be separated from "
                  "merged ones. Cherry-pick them onto the new branch yourself.")
    g.info(t.note)
    if t.cls == "DETACHED":
        g.branch_at_head(t, name)
    else:
        # A live branch keeps its own commits; only uncommitted work moves.
        g.new_branch(t, name, t.carry if t.cls in ("ON_DEFAULT", "MERGED", "CLOSED") else "")
    t.state, t.branch = "BRANCHED", name


if __name__ == "__main__":
    g.run_task("branch", body)
