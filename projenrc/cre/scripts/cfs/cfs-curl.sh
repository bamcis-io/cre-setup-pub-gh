#!/usr/bin/env bash
# npx projen cfs:curl <GET|POST|PUT|DELETE> <url-or-path> [body]
#
# Call anything behind a CloudFront Signer (CFS) distribution from a terminal or an agent session,
# with no browser. Performs the CFS V2 handshake with your Midway cookie, then makes the request
# with the signed CloudFront cookies.
#
#   npx projen cfs:curl GET https://dxxxx.cloudfront.net/stats/requests
#   CFS_URL=https://dxxxx.cloudfront.net npx projen cfs:curl POST /swarm 'user_count=10&spawn_rate=5'
#
# Prerequisites: `mwinit -f` (~/.midway/cookie unexpired), and the distribution onboarded at
# https://cloudfrontsigner.ninjas.security.a2z.com/. Midway-only, so internal use only.
#
# Each call re-does the handshake (the signed cookies last ~15 min), so calls are self-contained.
# The cookies never touch disk or the process list: they live in a variable and are sent on stdin.
# Generalized from skills/locust-cfs/scripts/lc.sh.
set -euo pipefail

METHOD="${1:?usage: cfs-curl.sh <GET|POST|PUT|DELETE> <url-or-path> [body]}"
TARGET_ARG="${2:?usage: cfs-curl.sh <GET|POST|PUT|DELETE> <url-or-path> [body]}"
DATA="${3:-}"
MID="${MIDWAY_COOKIE:-$HOME/.midway/cookie}"
SIGNER="${CFS_SIGNER:-https://cloudfrontsigner.ninjas.security.a2z.com/v2/authorize}"

case "$TARGET_ARG" in
  http://* | https://*) URL="$TARGET_ARG" ;;
  /*) URL="${CFS_URL:?a path needs CFS_URL set to the distribution URL, e.g. https://dxxxx.cloudfront.net}${TARGET_ARG}" ;;
  *) echo "❌ '$TARGET_ARG' is neither a URL nor a /path." >&2; exit 2 ;;
esac

[ -f "$MID" ] || { echo "❌ Midway cookie not found at $MID — run 'mwinit -f'." >&2; exit 1; }

# Request-for-proof: a random rfp, sent as SHA-384(rfp); the signed cookies are bound to it.
RFP=$(python3 -c "import os;print(os.urandom(8).hex())")
NONCE=$(RFP="$RFP" python3 -c "import hashlib,os;print(hashlib.sha384(os.environ['RFP'].encode()).hexdigest())")
# Same target encoding as the proven lc.sh: the full request URL.
ENC=$(TARGET="$URL" python3 -c "import urllib.parse,os;print(urllib.parse.quote(os.environ['TARGET'],safe=''))")

# authorize → Midway SSO → back; the final redirect carries the signed params in the URL fragment.
FINAL=$(curl -sSL -b "$MID" -o /dev/null -w '%{url_effective}' "${SIGNER}?encodedTargetUrl=${ENC}&cfsNonce=${NONCE}")

COOKIE=$(RFP="$RFP" python3 - "$FINAL" <<'PY'
import os, sys, urllib.parse
frag = sys.argv[1].split('#', 1)[1] if '#' in sys.argv[1] else ''
q = dict(urllib.parse.parse_qsl(frag))
pol, kpid, sig, exp = q.get('policy', ''), q.get('kpid', ''), q.get('sig', ''), q.get('exp', '')
if not (pol and sig and kpid):
    sys.stderr.write("❌ CFS handshake failed (no signed params). Is Midway valid (mwinit -f), and is the "
                     "distribution onboarded with CloudFront Signer? Final URL: %s\n" % sys.argv[1])
    sys.exit(1)
print(f"CloudFront-Policy={pol}; CloudFront-Signature={sig}; CloudFront-Key-Pair-Id={kpid}; "
      f"CloudFront-Expiration={exp}; CloudFront-RFP={os.environ['RFP']}")
PY
)

ARGS=(-sS -X "$METHOD" -H @-)
[ -n "$DATA" ] && ARGS+=(--data "$DATA")
curl "${ARGS[@]}" "$URL" <<<"Cookie: $COOKIE"
