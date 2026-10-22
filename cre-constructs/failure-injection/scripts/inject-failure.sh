#!/usr/bin/env bash
# Inject failure by setting the error-rate SSM knob. Param name is parameterized
# (read it from the FailureInjectionParameter `ErrorRateParamName` CfnOutput).
set -euo pipefail
if [ $# -ne 3 ]; then
  echo "Usage: $0 <region> <param-name> <error-rate>"
  echo "  region:     AWS region (e.g., us-east-1)"
  echo "  param-name: SSM param (from the ErrorRateParamName CfnOutput)"
  echo "  error-rate: Percentage of requests to fail (0-100)"
  exit 1
fi
REGION="$1"; PARAM="$2"; RATE="$3"
if ! [[ "$RATE" =~ ^[0-9]+$ ]] || [ "$RATE" -lt 0 ] || [ "$RATE" -gt 100 ]; then
  echo "Error: error-rate must be an integer between 0 and 100"; exit 1
fi
aws ssm put-parameter --name "$PARAM" --value "$RATE" \
  --type String --overwrite --region "$REGION"
echo "Set ${PARAM} to ${RATE}% in ${REGION}"
