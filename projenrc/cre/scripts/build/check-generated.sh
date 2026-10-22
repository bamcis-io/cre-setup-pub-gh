#!/usr/bin/env bash
# check:generated — in CI, fail when the committed generated files (the pipeline, cre-stacks.json,
# package.json, tasks, steering, ...) aren't what `npx projen` makes from .projenrc.ts and the CDK
# app. Otherwise the build regenerates them first and `check:stack-plan` compares the app with a
# plan regenerated from that same app: it always passes, and the committed pipeline that GitLab or
# GitHub actually runs may be stale (review #15).
#
# Runs only on a clean checkout in CI ($CI set): locally a work-in-progress tree is expected to
# differ, and `npx projen` there is the fix, not a failure.
set -uo pipefail
if [ -z "${CI:-}" ]; then
  echo "check:generated: not in CI; skipped (run npx projen to regenerate)."
  exit 0
fi
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  echo "check:generated: the checkout isn't clean before regenerating; skipped:" >&2
  git status --porcelain >&2
  exit 0
fi
npx projen >/tmp/check-generated.log 2>&1 || { cat /tmp/check-generated.log >&2; echo "check:generated: npx projen failed." >&2; exit 1; }
changed=$(git status --porcelain)
if [ -n "$changed" ]; then
  echo "❌ The committed generated files are stale: npx projen changes these:" >&2
  echo "$changed" | sed 's/^/   /' >&2
  git --no-pager diff --stat >&2
  echo "   Run npx projen, commit what it changes (with the change that caused it), and push." >&2
  exit 1
fi
echo "✅ check:generated: the committed generated files are current."
