#!/usr/bin/env bash
# with-role.sh <ROLE_ARN-or-empty> <command...>
#
# Run a command with credentials for another account's role (external DNS). When the role is
# empty, or already the identity in use, the command runs with the current credentials. Otherwise
# the current credentials assume the role (role chaining: the DNS role trusts this account's deploy
# role, see setup/dns.sh) and the command gets the temporary credentials in its environment only.
#
# Used the same way locally, in GitLab CI and in GitHub Actions, so the cross-account path does not
# depend on CI-specific credential vending.
set -euo pipefail

ROLE="${1-}"
shift
[ $# -gt 0 ] || { echo "usage: with-role.sh <role-arn|''> <command...>" >&2; exit 2; }

if [ -z "$ROLE" ]; then
  exec "$@"
fi

CALLER=$(aws sts get-caller-identity --query Arn --output text) || {
  echo "❌ with-role: no AWS credentials to assume $ROLE from." >&2
  exit 1
}
ROLE_NAME=${ROLE##*/}
case "$CALLER" in
  *":assumed-role/$ROLE_NAME/"*)
    if [ "$(echo "$CALLER" | cut -d: -f5)" = "$(echo "$ROLE" | cut -d: -f5)" ]; then
      exec "$@"
    fi
    ;;
esac

CREDS=$(aws sts assume-role --role-arn "$ROLE" --role-session-name "cre-${PROJECT_NAME:-deploy}-dns" \
  --duration-seconds 3600 --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text) || {
  echo "❌ with-role: could not assume $ROLE from $CALLER. Check the role's trust policy (setup/dns.sh grants this account's deploy role)." >&2
  exit 1
}
read -r AK SK ST <<<"$CREDS"
exec env -u AWS_PROFILE AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST" "$@"
