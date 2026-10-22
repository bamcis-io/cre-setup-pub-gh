#!/usr/bin/env python3
"""npx projen git:status — read-only. Classifies the repository and says what `push` would do.
(It runs `git fetch`, which updates remote-tracking refs only.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cregit as g  # noqa: E402


def body(t, argv):
    g.preamble(t, argv)
    g.fetch()
    t.default = g.default_branch()
    g.classify(t)
    carry = f", carrying {len(g.rev_list(t.carry))} commit(s)" if t.carry else ""
    would = {
        "BOOTSTRAP": "commit on main and push it directly (no MR/PR)",
        "DETACHED": "create a new branch at HEAD, keeping its commits (needs --branch)",
        "ON_DEFAULT": f"create a new branch from origin/{t.default} (needs --branch){carry}",
        "MERGED": f"create a new branch from origin/{t.default} (needs --branch){carry}",
        "CLOSED": f"create a new branch from origin/{t.default} (needs --branch){carry}",
        "MERGED_UNKNOWN_BASE": "refuse: new commits can't be separated from merged ones",
        "ACTIVE": f"commit and push to {t.current}",
        "NEW": f"commit and push to {t.current}",
    }[t.cls]
    lines = [f"Provider:       {t.provider.name}", f"Default branch: {t.default or '<none on origin>'}",
             f"Current:        {t.current or '<detached>'}", f"State:          {t.cls} — {t.note}", f"Push would:     {would}"]
    if t.default and t.current and not g.is_protected(t, t.current):
        conflicts = g.conflicts_with_default(t)
        if conflicts:
            lines.append(f"Conflicts with origin/{t.default}:\n" + g.indent(conflicts, " " * 16))
    if g.is_dirty():
        lines.append("Working tree:   uncommitted changes present")
    print("\n".join(lines), file=sys.stderr)
    t.state, t.branch = t.cls, t.current


if __name__ == "__main__":
    g.run_task("git:status", body)
