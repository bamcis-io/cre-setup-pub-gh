#!/usr/bin/env bash
# Restore = inject 0. Param name is parameterized (read it from the
# FailureInjectionParameter `ErrorRateParamName` CfnOutput).
#
# Takes one or more param names, or `--demo <demoName>` to reset every knob the construct
# creates under /<demoName>/ (the primary `error-rate` and each `additionalKnobs` entry's
# `<name>-error-rate`). Restoring only the primary left a demo with additional knobs degraded.
set -euo pipefail
usage() {
  echo "Usage: $0 <region> <param-name> [<param-name> ...]"
  echo "       $0 <region> --demo <demoName>"
  echo "  region:     AWS region (e.g., us-east-1)"
  echo "  param-name: SSM param (from the ErrorRateParamName CfnOutput)"
  echo "  --demo:     reset every /<demoName>/*error-rate knob"
  exit 1
}
[ $# -ge 2 ] || usage
REGION="$1"; shift
if [ "$1" = "--demo" ]; then
  [ $# -eq 2 ] || usage
  # A custom `parameterName` outside /<demoName>/ isn't found here; pass it explicitly.
  PARAMS=()
  while IFS= read -r name; do
    [ -n "$name" ] && PARAMS+=("$name")
  done < <(aws ssm get-parameters-by-path --path "/$2" --region "$REGION" \
             --query "Parameters[?ends_with(Name, 'error-rate')].Name" --output text | tr '\t' '\n')
  [ "${#PARAMS[@]}" -gt 0 ] || { echo "No */error-rate knobs found under /$2 in $REGION"; exit 1; }
else
  PARAMS=("$@")
fi
for PARAM in "${PARAMS[@]}"; do
  aws ssm put-parameter --name "$PARAM" --value "0" \
    --type String --overwrite --region "$REGION" >/dev/null
  echo "Restored ${PARAM} to 0% in ${REGION}"
done
