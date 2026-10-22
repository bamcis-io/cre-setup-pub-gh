#!/usr/bin/env bash
# synth:silent — `cdk synth -q`, accepting an app with no stacks.
#
# The CDK CLI exits 1 with "This app contains no stacks" for an app that synthesizes none, after
# the app ran and wrote cdk.out (manifest.json, and CreApp's cre-stacks.json). A fresh scaffold has
# no stacks on purpose (the project's design adds them), so that one case is a success here; every
# other failure is passed through with its output and exit code.
set -uo pipefail
out=$(cdk synth -q "$@" 2>&1)
rc=$?
if [ $rc -eq 0 ]; then
  [ -z "$out" ] || printf '%s\n' "$out"
  exit 0
fi
if printf '%s\n' "$out" | grep -q 'This app contains no stacks' && [ -f cdk.out/manifest.json ]; then
  echo "cdk synth: the app has no stacks yet (nothing to deploy)."
  exit 0
fi
printf '%s\n' "$out" >&2
exit $rc
