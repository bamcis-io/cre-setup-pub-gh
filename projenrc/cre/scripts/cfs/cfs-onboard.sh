#!/usr/bin/env bash
# npx projen cfs:onboard  [--domain <dxxxx.cloudfront.net>] [--profile <aws-profile>] [--status | --offboard]
#
# Onboard the project's CloudFront distribution at CloudFrontSigner, the one-time step after the
# first edge deploy that otherwise needs the web portal (https://cloudfrontsigner.ninjas.security.a2z.com).
#
# The portal's API is Midway-only: every call carries the Midway session cookie, and writes also
# carry an anti-CSRF token that GET /get-csrf-token returns in its `anti-csrftoken-a2z` header
# (both read from the portal's own bundle). Calls used:
#   GET  /get-domain-owner?domain=<d>   200 = onboarded (owner returned), 404 = not onboarded
#   POST /onboard   {"Domain": "<d>"}   -> {"DomainOnboarded": true}
#   POST /offboard  {"Domain": "<d>"}   -> {"DomainOffboarded": true}
#
# Domain: --domain, else the EdgeDomainName output of the project's CreEdgeStack (the one stack of
# kind edge in cre-stacks.json; always us-east-1), read with the AWS CLI (--profile or the ambient
# credentials).
#
# Exit codes: 0 onboarded (or already) / offboarded / status printed | 10 bad input or no domain |
# 11 Midway session missing or expired | 20 the portal refused or failed.
set -uo pipefail

die() { echo "❌ $2" >&2; echo "RESULT code=$1 step=cfs-onboard"; exit "$1"; }
BASE="${CFS_PORTAL_URL:-https://cloudfrontsigner.ninjas.security.a2z.com}"
COOKIE="${MIDWAY_COOKIE:-$HOME/.midway/cookie}"
DOMAIN="" PROFILE="" MODE=onboard
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --profile) PROFILE="${2:-}"; shift 2 ;;
    --status) MODE=status; shift ;;
    --offboard) MODE=offboard; shift ;;
    *) die 10 "Unknown argument: $1 (usage: cfs:onboard [--domain d] [--profile p] [--status|--offboard])" ;;
  esac
done

if [ -z "$DOMAIN" ]; then
  # The CreEdgeStack in the stack plan (kind edge; check:stack-plan keeps it in us-east-1).
  STACK=$(python3 -c 'import json,sys
try: stacks = json.load(open("cre-stacks.json")).get("stacks", [])
except Exception: stacks = []
edges = [s["id"] for s in stacks if s.get("kind") == "edge"]
print(edges[0] if len(edges) == 1 else ("" if not edges else "MANY:" + " ".join(edges)))')
  case "$STACK" in
    "") die 10 "No --domain, and cre-stacks.json has no CreEdgeStack. Pass --domain <dxxxx.cloudfront.net>." ;;
    MANY:*) die 10 "cre-stacks.json has several edge stacks (${STACK#MANY:}); pass --domain for the one to onboard." ;;
  esac
  DOMAIN=$(aws ${PROFILE:+--profile "$PROFILE"} cloudformation describe-stacks --region us-east-1 --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='EdgeDomainName'].OutputValue" --output text 2>/dev/null) || DOMAIN=""
  [ -n "$DOMAIN" ] && [ "$DOMAIN" != None ] ||
    die 10 "No --domain, and could not read EdgeDomainName from stack $STACK in us-east-1${PROFILE:+ (profile $PROFILE)}. Deploy the edge first, or pass --domain."
fi
echo "$DOMAIN" | grep -Eq '^[a-z0-9]+\.cloudfront\.net$|^([a-z0-9-]+\.)+[a-z]{2,}$' || die 10 "'$DOMAIN' is not a domain name."

# Midway: the same expiry check the setup scripts use.
EXP=""
[ -f "$COOKIE" ] && EXP=$(awk '/midway-auth.amazon.com.*\tsession\t/ {print $5}' "$COOKIE" | sort -n | tail -1)  # the latest session line
if [ -z "$EXP" ] || [ "$(date +%s)" -gt "$EXP" ]; then
  die 11 "Midway session missing or expired. Run: mwinit -f -k ~/.ssh/id_ecdsa.pub   then re-run."
fi

# A per-run jar seeded from the Midway cookie: the portal's front door sets its own session cookie
# on the first GET, and writes without it are bounced to sign-in.
JAR=$(mktemp) HDR=$(mktemp) OUT=$(mktemp)
trap 'rm -f "$JAR" "$HDR" "$OUT"' EXIT
cp "$COOKIE" "$JAR"
curl -sS -L -o /dev/null -b "$JAR" -c "$JAR" "$BASE/" 2>/dev/null || die 20 "Cannot reach $BASE."

owner() { # sets CODE and OUT
  CODE=$(curl -sS -L -o "$OUT" -w '%{http_code}' -b "$JAR" -c "$JAR" -H 'Accept: application/json' \
    "$BASE/get-domain-owner?domain=$DOMAIN" 2>/dev/null) || CODE=000
}
owner
case "$CODE" in
  200) ONBOARDED=1 ;;
  404) ONBOARDED="" ;;
  401 | 403) die 11 "CloudFrontSigner rejected the Midway session ($CODE). Run mwinit -f -k ~/.ssh/id_ecdsa.pub and re-run." ;;
  *) die 20 "get-domain-owner for $DOMAIN returned $CODE: $(head -c 300 "$OUT")" ;;
esac

if [ "$MODE" = status ]; then
  if [ -n "$ONBOARDED" ]; then echo "✅ $DOMAIN is onboarded: $(head -c 300 "$OUT")" >&2; echo "ONBOARDED=true"
  else echo "ℹ  $DOMAIN is not onboarded." >&2; echo "ONBOARDED=false"; fi
  echo "RESULT code=0 step=cfs-onboard"; exit 0
fi
if [ "$MODE" = onboard ] && [ -n "$ONBOARDED" ]; then
  echo "✅ $DOMAIN is already onboarded at CloudFrontSigner." >&2
  echo "RESULT code=0 step=cfs-onboard"; exit 0
fi
if [ "$MODE" = offboard ] && [ -z "$ONBOARDED" ]; then
  echo "✅ $DOMAIN is not onboarded; nothing to offboard." >&2
  echo "RESULT code=0 step=cfs-onboard"; exit 0
fi

CODE=$(curl -sS -L -D "$HDR" -o /dev/null -w '%{http_code}' -b "$JAR" -c "$JAR" -H 'anti-csrftoken-a2z-request: true' \
  "$BASE/get-csrf-token" 2>/dev/null) || CODE=000
TOKEN=$(tr -d '\r' <"$HDR" | sed -n 's/^[Aa]nti-[Cc]srftoken-[Aa]2z: *//p' | tail -1)
[ "$CODE" = 200 ] && [ -n "$TOKEN" ] || die 20 "Could not get a CSRF token from CloudFrontSigner ($CODE)."

BODY=$(python3 -c 'import json,sys;print(json.dumps({"Domain":sys.argv[1]}))' "$DOMAIN")
CODE=$(printf 'anti-csrftoken-a2z: %s\n' "$TOKEN" | curl -sS -L -o "$OUT" -w '%{http_code}' -b "$JAR" -c "$JAR" \
  -H 'Content-Type: application/json' -H @- -X POST --data "$BODY" "$BASE/$MODE" 2>/dev/null) || CODE=000
FIELD=$([ "$MODE" = onboard ] && echo DomainOnboarded || echo DomainOffboarded)
if [ "$CODE" = 200 ] && python3 -c 'import json,sys;sys.exit(0 if json.load(open(sys.argv[1])).get(sys.argv[2]) is True else 1)' "$OUT" "$FIELD" 2>/dev/null; then
  echo "✅ $([ "$MODE" = onboard ] && echo Onboarded || echo Offboarded) $DOMAIN at CloudFrontSigner." >&2
  echo "RESULT code=0 step=cfs-onboard"; exit 0
fi
MSG=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("Message",""))
except Exception: print(open(sys.argv[1]).read()[:300])' "$OUT")
die 20 "CloudFrontSigner $MODE of $DOMAIN failed ($CODE): ${MSG:-no message}"
