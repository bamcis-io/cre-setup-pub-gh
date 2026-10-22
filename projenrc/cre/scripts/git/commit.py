#!/usr/bin/env python3
"""npx projen commit --message "<conventional commit>" [--files "<paths>"] [--branch <new>]

Validate (synth + build), stage, commit. Never pushes. Moves to a new branch first when the current
one can't take the commit (default branch, merged, closed, detached).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cregit as g  # noqa: E402


def body(t, argv):
    g.preamble(t, argv)
    g.check_message_and_title(t.args)
    g.fetch()
    t.default = g.default_branch()
    g.classify(t)
    if t.cls != "BOOTSTRAP":
        g.resolve_branch(t, t.args.branch)
    t.branch = g.gout("branch", "--show-current")
    g.validate(t)
    g.stage(t)
    if g.commit(t):
        t.state = "COMMITTED"
    else:
        t.state = "NOTHING_TO_COMMIT"
        g.info("Nothing to commit.")


if __name__ == "__main__":
    g.run_task("commit", body)
