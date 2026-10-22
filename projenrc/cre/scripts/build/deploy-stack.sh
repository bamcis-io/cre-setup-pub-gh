#!/usr/bin/env bash
#
# Deploy one CloudFormation stack using a template URL pointing at content
# previously uploaded by the `deploy:s3` projen task. Always uses
# --template-url so the 51200-byte local body limit never applies.
#
# `aws cloudformation deploy` does NOT support --template-url, so we drive
# change-sets directly: create-change-set → wait → execute-change-set →
# poll-until-transitioning → wait-terminal. An empty change-set is treated
# as success (matches the old --no-fail-on-empty-changeset behavior).
#
# Required env:
#   AWS_REGION          Target region for this deploy. The assets bucket
#                       also lives in this region — content is uploaded to
#                       one bucket per region during the upload phase.
#   STACK_NAME          Physical CFN stack name.
#   TEMPLATE_NAME       Base name (no extension) of the top-level template
#                       inside the S3 prefix (the stack's deployed name). The
#                       deploy:stack runs this once per stack (one CI job each).
#   ASSETS_BUCKET       Bucket in $AWS_REGION populated by the upload phase.
#   ASSETS_PREFIX       Matching key prefix. Trailing slash added if missing.
#
# Optional env:
#   STACK_PARAMETERS    Caller-supplied CFN params, one per line as
#                       `Key=Value` (newline-separated, NOT space).
#                       Values may contain commas, spaces, and any other
#                       character except newline and the first '='. The
#                       script converts them into a JSON parameters file
#                       (so CLI shorthand parsing never sees the values).
#                       AssetsBucketName / AssetsBucketPrefix are always
#                       appended automatically.
#   OUTPUTS_FILE        Dotenv file for stack outputs. GitLab CI consumes
#                       this via `artifacts: reports: dotenv:`.
#                       Defaults to dist/$STACK_NAME.env.
#   OUTPUTS_PREFIX      Prefix prepended to every exported key so multi-job
#                       dotenv merges don't collide (e.g. DESTA_APIURL vs
#                       DESTB_APIURL). Defaults to "" (no prefix).
#   CAPABILITIES        Override `--capabilities`. Default:
#                         CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND
#   CHANGESET_PREFIX    Prefix for the generated change-set name.
#                       Defaults to "ci".
#   CLEANUP_ON_FAILURE  If "true", delete the uploaded S3 prefix when the
#                       deploy fails. Off by default: the prefix is shared by
#                       every stack in the run (and by per-stack GitLab jobs).
#   ROLE_ARN            CloudFormation's service role. Empty: CFN_EXECUTION_ROLE_ARN.
#   ASSUME_ROLE         A role to assume first (role chaining, with-role.sh), e.g.
#                       the DNS account's deploy role. Empty: the current identity.
#   OUTPUTS             Space-separated output names to write to OUTPUTS_FILE (the
#                       ones later stacks take). Unset: every output. GitLab caps a
#                       dotenv artifact's variable count, so jobs pass only these.
#   REQUIRED_VARS       Space-separated variable names that must be non-empty.
#   REQUIRED_OUTPUTS    Space-separated `<VAR>=<stack>/<Output>`: the earlier
#                       stacks' outputs this one takes; each VAR must be set.
#   STACK_WAIT_SECONDS  How long to wait for the stack to finish (default 6000, under the
#                       2-hour GitLab deploy job timeout, so a slow stack fails here, with its
#                       events, instead of the job being killed mid-wait).
#
# Every stack job sets all of these from the stacks' plan (stack-jobs.ts in the
# project type); this script checks them and deploys. It looks nothing up.

set -euo pipefail

# An unset CI secret or variable arrives as an empty string, which would otherwise surface as a
# CloudFormation parameter error (or a deploy as the wrong role) much later.
for v in ${REQUIRED_VARS:-}; do
  if [ -z "${!v:-}" ]; then
    echo "$v is set but EMPTY - an unset CI secret or variable arrives as an empty string. Set it and re-run." >&2
    exit 1
  fi
done
for r in ${REQUIRED_OUTPUTS:-}; do
  var="${r%%=*}" what="${r#*=}"
  if [ -z "${!var+set}" ]; then
    echo "stack ${what%%/*} has no output ${what#*/} (needed by ${STACK_NAME:-this stack})" >&2
    exit 1
  fi
done
ROLE_ARN="${ROLE_ARN:-${CFN_EXECUTION_ROLE_ARN:-}}"
export ROLE_ARN
if [ -n "${ASSUME_ROLE:-}" ] && [ -z "${CRE_ROLE_ASSUMED:-}" ]; then
  exec env CRE_ROLE_ASSUMED=1 bash "$(dirname "${BASH_SOURCE[0]}")/with-role.sh" "$ASSUME_ROLE" bash "${BASH_SOURCE[0]}"
fi

: "${AWS_REGION:?AWS_REGION is required}"
: "${STACK_NAME:?STACK_NAME is required}"
: "${TEMPLATE_NAME:?TEMPLATE_NAME is required}"
: "${ASSETS_BUCKET:?ASSETS_BUCKET is required}"
: "${ASSETS_PREFIX:?ASSETS_PREFIX is required}"

OUTPUTS_FILE="${OUTPUTS_FILE:-dist/${STACK_NAME}.env}"
OUTPUTS_PREFIX="${OUTPUTS_PREFIX:-}"
CAPABILITIES="${CAPABILITIES:-CAPABILITY_IAM CAPABILITY_NAMED_IAM CAPABILITY_AUTO_EXPAND}"
CHANGESET_PREFIX="${CHANGESET_PREFIX:-ci}"
STACK_PARAMETERS="${STACK_PARAMETERS:-}"
CLEANUP_ON_FAILURE="${CLEANUP_ON_FAILURE:-false}"
STACK_WAIT_SECONDS="${STACK_WAIT_SECONDS:-6000}"
STACK_POLL_SECONDS="${STACK_POLL_SECONDS:-15}"

case "$ASSETS_PREFIX" in
  */) ;;
  *)  ASSETS_PREFIX="${ASSETS_PREFIX}/" ;;
esac

# Regional S3 endpoint. The assets bucket is deployed per-region and
# $AWS_REGION is the stack's deploy region — which, by construction, is also
# the region the bucket lives in. Virtual-hosted style is the most
# compatible form for cross-feature S3 requests (CloudFormation included).
# The S3 domain differs by partition (review #11): China, the European Sovereign Cloud.
case "$AWS_REGION" in
  cn-*) S3_DOMAIN=amazonaws.com.cn ;;
  eusc-*) S3_DOMAIN=amazonaws.eu ;;
  *) S3_DOMAIN=amazonaws.com ;;
esac
TEMPLATE_URL="https://${ASSETS_BUCKET}.s3.${AWS_REGION}.${S3_DOMAIN}/${ASSETS_PREFIX}${TEMPLATE_NAME}.json"

# ---- helpers ----------------------------------------------------------------

# The stack's status, or empty when it doesn't exist. Any other failure (throttling, expired or
# missing credentials, a denied call) stops the deploy: read as "absent", it would create a stack
# that exists, or pass with no outputs. Retries throttling a few times first.
describe_status() {
  local out tries=0
  while :; do
    if out=$(aws cloudformation describe-stacks --region "$AWS_REGION" --stack-name "$STACK_NAME" \
      --query 'Stacks[0].StackStatus' --output text 2>&1); then
      echo "$out"
      return 0
    fi
    case "$out" in
      *"does not exist"*) echo ""; return 0 ;;
      *Throttl* | *"Rate exceeded"* | *RequestLimitExceeded*)
        tries=$((tries + 1))
        if [ "$tries" -lt 5 ]; then sleep $((tries * 5)); continue; fi ;;
    esac
    echo "describe-stacks $STACK_NAME failed: $out" >&2
    return 1
  done
}

# The stack's latest failed events, so a failed deploy says why (not only its final status).
print_failures() {
  echo "Failed resources of $STACK_NAME (latest first):" >&2
  aws cloudformation describe-stack-events --region "$AWS_REGION" --stack-name "$STACK_NAME" \
    --query 'StackEvents[?contains(ResourceStatus, `FAILED`)] | [0:10].[Timestamp,LogicalResourceId,ResourceType,ResourceStatus,ResourceStatusReason]' \
    --output text >&2 2>/dev/null || echo "  (could not read the stack events)" >&2
}

# wait_settled [deadline-seconds] — wait while the stack is *_IN_PROGRESS (not REVIEW_IN_PROGRESS);
# prints the final status.
wait_settled() {
  local deadline=$(( $(date +%s) + ${1:-$STACK_WAIT_SECONDS} )) st
  while :; do
    st=$(describe_status) || return 1
    case "$st" in
      REVIEW_IN_PROGRESS | "") echo "$st"; return 0 ;;
      *_IN_PROGRESS) ;;
      *) echo "$st"; return 0 ;;
    esac
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "Stack $STACK_NAME still $st after waiting; giving up." >&2
      return 1
    fi
    sleep "$STACK_POLL_SECONDS"
  done
}

cleanup_on_failure() {
  if [ "$CLEANUP_ON_FAILURE" = "true" ]; then
    echo "Cleaning up s3://$ASSETS_BUCKET/$ASSETS_PREFIX after failure"
    aws s3 rm "s3://$ASSETS_BUCKET/$ASSETS_PREFIX" --recursive || true
  fi
}

# ---- change-set type --------------------------------------------------------

INITIAL_STATUS="$(describe_status)" || exit 1

# Still running from another run (a retried or timed-out job, a concurrent pipeline): a change
# set can't be created until it settles, so wait for it, then deploy from where it ended.
case "$INITIAL_STATUS" in
  REVIEW_IN_PROGRESS) ;;
  *_IN_PROGRESS)
    echo "Stack $STACK_NAME is $INITIAL_STATUS (another deploy, or one that was interrupted); waiting for it to finish."
    INITIAL_STATUS="$(wait_settled)" || exit 1
    echo "Stack $STACK_NAME is now ${INITIAL_STATUS:-<gone>}."
    ;;
esac
case "$INITIAL_STATUS" in
  UPDATE_ROLLBACK_FAILED)
    echo "Stack $STACK_NAME is UPDATE_ROLLBACK_FAILED: CloudFormation refuses any update until the rollback is continued. Fix the resource named below, then: aws cloudformation continue-update-rollback --region $AWS_REGION --stack-name $STACK_NAME   and re-run." >&2
    print_failures
    exit 1 ;;
  DELETE_FAILED)
    echo "Stack $STACK_NAME is DELETE_FAILED; delete what blocks it (below), then delete the stack and re-run." >&2
    print_failures
    exit 1 ;;
esac

# A first create that failed leaves the stack in ROLLBACK_COMPLETE (or ROLLBACK_FAILED), which
# CloudFormation refuses to update — so without this every later pipeline stays red until
# someone deletes the stack by hand. The stack never existed successfully, so deleting it loses
# nothing (`cdk deploy` does the same).
case "$INITIAL_STATUS" in
  ROLLBACK_COMPLETE|ROLLBACK_FAILED)
    echo "Stack $STACK_NAME is $INITIAL_STATUS from a failed first create; deleting it before retrying."
    aws cloudformation delete-stack --region "$AWS_REGION" --stack-name "$STACK_NAME"
    aws cloudformation wait stack-delete-complete --region "$AWS_REGION" --stack-name "$STACK_NAME"
    INITIAL_STATUS="$(describe_status)" || exit 1
    ;;
esac

if [ -z "$INITIAL_STATUS" ]; then
  CS_TYPE=CREATE
else
  case "$INITIAL_STATUS" in
    REVIEW_IN_PROGRESS) CS_TYPE=CREATE ;;
    *)                  CS_TYPE=UPDATE ;;
  esac
fi

CHANGESET_NAME="${CHANGESET_PREFIX}-$(date +%s)-${RANDOM}"

# Build a JSON parameters file. This sidesteps the AWS CLI shorthand parser
# entirely, so values with commas, quotes, or spaces pass through cleanly.
# Caller-supplied pairs come first; AssetsBucket* are appended unconditionally
# because package.py rewrote nested templates to require them.
PARAMS_FILE="dist/${STACK_NAME}.params.json"
mkdir -p "$(dirname "$PARAMS_FILE")"

{
  # Real newlines from every caller (GitLab, GitHub and the local deploy render the lines with
  # them), so the value goes through untouched: a backslash in a parameter stays a backslash.
  printf '%s\n' "$STACK_PARAMETERS"
  printf 'AssetsBucketName=%s\n' "$ASSETS_BUCKET"
  printf 'AssetsBucketPrefix=%s\n' "$ASSETS_PREFIX"
} | python3 -c '
import json
import sys

params = []
for line in sys.stdin:
    line = line.rstrip("\n")
    if not line.strip():
        continue
    # Split on the FIRST = only; value may contain more = chars.
    if "=" not in line:
        print(f"Malformed STACK_PARAMETERS line (missing =): {line!r}", file=sys.stderr)
        sys.exit(1)
    key, _, value = line.partition("=")
    params.append({"ParameterKey": key.strip(), "ParameterValue": value})
json.dump(params, sys.stdout)
' > "$PARAMS_FILE"

echo "Deploying $STACK_NAME to $AWS_REGION ($CS_TYPE)"
echo "  template:   $TEMPLATE_URL"
echo "  changeset:  $CHANGESET_NAME"
echo "  params:     $PARAMS_FILE"
echo "  initial:    ${INITIAL_STATUS:-<stack does not exist>}"

# ---- create change-set ------------------------------------------------------

# shellcheck disable=SC2086  # CAPABILITIES intentionally word-split
ROLE_ARN_FLAG=""
if [ -n "$ROLE_ARN" ]; then
  ROLE_ARN_FLAG="--role-arn $ROLE_ARN"
fi

aws cloudformation create-change-set \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --change-set-name "$CHANGESET_NAME" \
  --change-set-type "$CS_TYPE" \
  --template-url "$TEMPLATE_URL" \
  --parameters "file://$PARAMS_FILE" \
  --capabilities $CAPABILITIES \
  $ROLE_ARN_FLAG \
  >/dev/null

# Wait for change-set creation. The waiter fails when a change-set completes
# empty (no diffs) — we detect that via describe-change-set and call it done.
set +e
aws cloudformation wait change-set-create-complete \
  --region "$AWS_REGION" \
  --stack-name "$STACK_NAME" \
  --change-set-name "$CHANGESET_NAME"
WAIT_RC=$?
set -e

EMPTY_CHANGESET=false
if [ "$WAIT_RC" -ne 0 ]; then
  DESC=$(aws cloudformation describe-change-set \
            --region "$AWS_REGION" \
            --stack-name "$STACK_NAME" \
            --change-set-name "$CHANGESET_NAME" \
            --query '{Status:Status,StatusReason:StatusReason}' \
            --output json)
  if echo "$DESC" | grep -qE "(didn't contain changes|No updates|The submitted information didn't contain changes)"; then
    echo "No changes to deploy — treating empty change-set as success."
    EMPTY_CHANGESET=true
    aws cloudformation delete-change-set \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME" \
      --change-set-name "$CHANGESET_NAME" >/dev/null 2>&1 || true
    # An empty CREATE leaves a REVIEW_IN_PROGRESS stack shell behind —
    # clean it up so the next run starts fresh.
    if [ "$CS_TYPE" = "CREATE" ]; then
      aws cloudformation delete-stack \
        --region "$AWS_REGION" \
        --stack-name "$STACK_NAME" >/dev/null 2>&1 || true
    fi
  else
    echo "Change-set creation failed:" >&2
    echo "$DESC" >&2
    cleanup_on_failure
    exit 1
  fi
fi

# ---- execute + wait ---------------------------------------------------------

if [ "$EMPTY_CHANGESET" = false ]; then
  echo "Executing change-set $CHANGESET_NAME"
  aws cloudformation execute-change-set \
    --region "$AWS_REGION" \
    --stack-name "$STACK_NAME" \
    --change-set-name "$CHANGESET_NAME"

  # Before waiting for the terminal state, poll until the stack's status
  # actually transitions away from its prior value. Without this, a wait
  # against a stack that was already in CREATE_COMPLETE / UPDATE_COMPLETE
  # can race and return before CFN has switched it to _IN_PROGRESS,
  # producing a false success.
  #
  # The stack status alone can miss the transition: a small update can go
  # UPDATE_COMPLETE → UPDATE_IN_PROGRESS → UPDATE_COMPLETE inside one sleep,
  # and the loop would then spin until the CI job timed out. The change-set's
  # own ExecutionStatus reaching a terminal value proves the execution
  # happened, so it ends the poll too. The deadline turns any remaining hang
  # into a clear failure instead of a silent one.
  echo "Waiting for stack to begin transitioning from ${INITIAL_STATUS:-<none>}"
  TRANSITION_DEADLINE=$(( $(date +%s) + 600 ))
  while true; do
    CURRENT="$(describe_status)" || { cleanup_on_failure; exit 1; }
    if [ -n "$CURRENT" ] && [ "$CURRENT" != "${INITIAL_STATUS:-}" ]; then
      echo "Stack transitioned to: $CURRENT"
      break
    fi
    EXEC_STATUS=$(aws cloudformation describe-change-set \
                    --region "$AWS_REGION" \
                    --stack-name "$STACK_NAME" \
                    --change-set-name "$CHANGESET_NAME" \
                    --query 'ExecutionStatus' --output text 2>/dev/null || echo "")
    case "$EXEC_STATUS" in
      EXECUTE_COMPLETE|EXECUTE_FAILED)
        echo "Change-set execution finished ($EXEC_STATUS) between polls; stack is ${CURRENT:-<gone>}"
        break ;;
    esac
    if [ "$(date +%s)" -ge "$TRANSITION_DEADLINE" ]; then
      echo "Stack never left ${INITIAL_STATUS:-<none>} within 600s of executing $CHANGESET_NAME (change-set: ${EXEC_STATUS:-<unknown>})" >&2
      cleanup_on_failure
      exit 1
    fi
    sleep 5
  done

  # Our own bounded wait (the CLI waiters stop at 60 minutes, GitLab's default job timeout).
  FINAL="$(wait_settled)" || { print_failures; cleanup_on_failure; exit 1; }
  case "$FINAL" in
    CREATE_COMPLETE | UPDATE_COMPLETE) ;;
    *)
      echo "Stack $STACK_NAME ended ${FINAL:-<gone>}, not CREATE_COMPLETE / UPDATE_COMPLETE." >&2
      print_failures
      cleanup_on_failure
      exit 1 ;;
  esac
fi

# ---- capture outputs --------------------------------------------------------

STACK_STATUS="$(describe_status)" || exit 1
case "$STACK_STATUS" in
  CREATE_COMPLETE|UPDATE_COMPLETE|UPDATE_ROLLBACK_COMPLETE)
    mkdir -p "$(dirname "$OUTPUTS_FILE")"
    aws cloudformation describe-stacks \
      --region "$AWS_REGION" \
      --stack-name "$STACK_NAME" \
      --query 'Stacks[0].Outputs' \
      --output json \
      | python3 -c "
import json, os, sys
prefix = os.environ.get('OUTPUTS_PREFIX', '')
outputs = json.load(sys.stdin) or []
wanted = os.environ.get('OUTPUTS')
if wanted is not None:
    keep = set(wanted.split())
    outputs = [o for o in outputs if o['OutputKey'] in keep]
for o in outputs:
    key = (prefix + '_' if prefix else '') + o['OutputKey'].upper()
    value = o['OutputValue'].replace('\n', ' ')
    print(f'{key}={value}')
" > "$OUTPUTS_FILE"
    echo "Stack outputs written to $OUTPUTS_FILE"
    ;;
  "")
    echo "Stack $STACK_NAME not present after deploy (empty CREATE); no outputs to capture."
    # Still write the file: the stack job publishes it as its artifact for the jobs that need it.
    mkdir -p "$(dirname "$OUTPUTS_FILE")" && : > "$OUTPUTS_FILE"
    ;;
  *)
    echo "Unexpected stack status: $STACK_STATUS" >&2
    cleanup_on_failure
    exit 1
    ;;
esac
