#!/usr/bin/env bash
# Delete old deploy prefixes from the assets buckets, keeping the current deploy plus the two
# before it. Runs as the last deploy phase, only after every stack deployed.
#
#   PROJECT_NAME   prefixes live under s3://<bucket>/<PROJECT_NAME>/
#   ASSETS_PREFIX  this deploy's prefix (always kept)
#   PRUNE_BUCKETS  space-separated bucket names (one per region)
#   PRUNE_KEEP     previous prefixes to keep besides the current one (default 2)
#
# A prefix is a deploy prefix only if its name is a pipeline id (`<project>/<digits>/`, GitLab CI)
# or a UTC timestamp (`<project>/YYYY-MM-DDTHH-MM-SSZ/`, local and GitHub deploys). Anything else
# under the project, and anything outside it, is never touched. Prefixes are ordered by their
# newest object's LastModified, because the two naming schemes don't sort against each other.
#
# Never fails the deploy: problems are reported as warnings and the script exits 0. The stacks are
# already deployed by the time this runs, and the worst case of not pruning is a larger bucket.
set -uo pipefail

warn() { echo "⚠️  prune: $*" >&2; }
: "${PROJECT_NAME:?PROJECT_NAME not set}"
: "${ASSETS_PREFIX:?ASSETS_PREFIX not set}"
KEEP="${PRUNE_KEEP:-2}"
CURRENT="${ASSETS_PREFIX%/}/"

if [ -n "${CRE_SKIP_PRUNE:-}" ]; then
  echo "prune: skipped (CRE_SKIP_PRUNE set)"
  exit 0
fi

for BUCKET in ${PRUNE_BUCKETS:-}; do
  LISTING=$(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix "$PROJECT_NAME/" \
    --query 'Contents[].[Key,LastModified]' --output text 2>&1) || {
    warn "could not list s3://$BUCKET/$PROJECT_NAME/ ($LISTING); skipping this bucket"
    continue
  }
  DELETE=$(printf '%s\n' "$LISTING" | python3 -c '
import re, sys
project, current, keep = sys.argv[1], sys.argv[2], int(sys.argv[3])
pat = re.compile(r"^" + re.escape(project) + r"/(\d+|\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)/")
newest = {}
for line in sys.stdin:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 2 or parts[0] == "None":
        continue
    m = pat.match(parts[0])
    if not m:
        continue
    prefix = m.group(0)
    newest[prefix] = max(newest.get(prefix, ""), parts[1])
others = sorted((p for p in newest if p != current), key=lambda p: newest[p], reverse=True)
for p in others[keep:]:
    print(p)
' "$PROJECT_NAME" "$CURRENT" "$KEEP") || {
    warn "could not parse the listing for $BUCKET; skipping"
    continue
  }
  if [ -z "$DELETE" ]; then
    echo "prune: s3://$BUCKET — nothing to delete (keeping $CURRENT + $KEEP previous)"
    continue
  fi
  while IFS= read -r P; do
    [ -n "$P" ] || continue
    if aws s3 rm --recursive --only-show-errors "s3://$BUCKET/$P"; then
      echo "prune: deleted s3://$BUCKET/$P"
    else
      warn "failed to delete s3://$BUCKET/$P"
    fi
  done <<<"$DELETE"
done
exit 0
