#!/usr/bin/env bash
#
# Push the pre-built image tarballs to ECR in every target region.
#
# Expects the build stage to have produced:
#   <CONTENT_DIR>/containers/manifest.json
#   <CONTENT_DIR>/containers/<hash>.tar.gz
#
# Uses `crane` (https://github.com/google/go-containerregistry) — a
# single static Go binary that speaks the OCI registry protocol
# directly. No docker daemon, no user namespaces needed, so it works
# on restricted CI runners where dind/podman/buildah all failed.
#
# Idempotent: skips ECR repos that already exist, skips pushes when the
# exact content-hashed tag is already present.
#
# Target regions, per image: each image goes to the regions of the stacks that use it. The
# manifest records the stacks (container-plan.sh, from each <stack>.assets.json); --stack-regions
# (what deploy:docker passes, generated from the stacks' regions) says where each stack deploys. An image
# used by a stack the mapping doesn't name is an error.
#
# Usage: deploy-docker.sh --stack-regions "<stack>=<region> ..."
#
# Optional env:
#   CONTENT_DIR            Where deploy:s3 unpacked content.zip. Defaults to dist/content.
#   DRY_RUN                "true" to print actions and exit.

set -euo pipefail

STACK_REGIONS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stack-regions) STACK_REGIONS="${2:?--stack-regions needs \"<stack>=<region> ...\"}"; shift 2 ;;
    *) echo "deploy-docker.sh: unknown argument '$1' (usage: --stack-regions \"<stack>=<region> ...\")" >&2; exit 64 ;;
  esac
done
[ -n "$STACK_REGIONS" ] || { echo "deploy-docker.sh: --stack-regions is required" >&2; exit 64; }
export STACK_REGIONS

CONTENT_DIR="${CONTENT_DIR:-dist/content}"
DRY_RUN="${DRY_RUN:-false}"
MANIFEST="$CONTENT_DIR/containers/manifest.json"

# ---- helpers --------------------------------------------------------------

ensure_repo() {
  local region="$1"
  local repo="$2"
  if aws ecr describe-repositories --region "$region" --repository-names "$repo" >/dev/null 2>&1; then
    echo "  repo exists"
    return
  fi
  if [ "$DRY_RUN" = "true" ]; then
    echo "  (dry-run: would create repo $repo)"
    return
  fi
  aws ecr create-repository \
    --region "$region" \
    --repository-name "$repo" \
    --image-scanning-configuration scanOnPush=true \
    --encryption-configuration encryptionType=AES256 \
    >/dev/null
  echo "  repo created"
}

image_exists() {
  local region="$1"
  local repo="$2"
  local tag="$3"
  aws ecr describe-images \
    --region "$region" \
    --repository-name "$repo" \
    --image-ids "imageTag=$tag" \
    >/dev/null 2>&1
}

# registry_host <region> — this account's ECR registry host in <region>, from ECR itself.
registry_host() {
  local endpoint
  endpoint=$(aws ecr get-authorization-token --region "$1" --query 'authorizationData[0].proxyEndpoint' --output text) ||
    { echo "ERROR: ecr get-authorization-token failed in $1" >&2; return 1; }
  echo "${endpoint#https://}"
}

# ---- preflight ------------------------------------------------------------

if [ ! -f "$MANIFEST" ]; then
  echo "Missing container manifest at $MANIFEST. Did build:docker run?" >&2
  exit 1
fi

COUNT=$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$MANIFEST")
if [ "$COUNT" -eq 0 ]; then
  echo "Manifest has zero images — nothing to push."
  exit 0
fi

if ! command -v crane >/dev/null 2>&1; then
  echo "ERROR: crane not found on PATH (the local deploy pushes images with it; CI installs it itself)." >&2
  echo "Install it: brew install crane   (or https://github.com/google/go-containerregistry/releases)" >&2
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying account: $ACCOUNT_ID"
echo "Pushing $COUNT image(s) from $CONTENT_DIR/containers/ via crane"

# ---- push loop ------------------------------------------------------------

# Emit one row per image with tab-separated fields:
#   hash, repository_template, tag_template, comma-separated regions (of the stacks using it).
python3 -c '
import json, os, sys
stack_regions = dict(p.split("=", 1) for p in os.environ["STACK_REGIONS"].split() if "=" in p)
for img in json.load(open(sys.argv[1])):
    users = img.get("stacks") or []
    unknown = [s for s in users if s not in stack_regions]
    if not users or unknown:
        sys.stderr.write("ERROR: image %s is used by %s, which cre-stacks.json does not name\n" % (img["hash"], unknown or "no recorded stack"))
        sys.exit(1)
    regions = list(dict.fromkeys(stack_regions[s] for s in users))
    print("\t".join([img["hash"], img["repository"], img["tag"], ",".join(regions)]))
' "$MANIFEST" | while IFS=$'\t' read -r IMAGE_HASH REPO_TEMPLATE TAG_TEMPLATE REGIONS_CSV; do
  TAR_PATH="$CONTENT_DIR/containers/${IMAGE_HASH}.tar.gz"
  if [ ! -f "$TAR_PATH" ]; then
    echo "ERROR: image tar not found: $TAR_PATH (manifest referenced $IMAGE_HASH)." >&2
    echo "  A local build without Docker skips the images (build:docker warns). Start Docker, run npx projen build, then deploy." >&2
    exit 1
  fi

  echo
  echo "=== $IMAGE_HASH ==="
  echo "  regions: $REGIONS_CSV"

  # crane push reads uncompressed tarballs, so decompress once and
  # reuse across regions. The gunzipped copy is cleaned up after the
  # region loop.
  UNCOMPRESSED="${TAR_PATH%.gz}"
  if [ ! -f "$UNCOMPRESSED" ]; then
    gunzip -k "$TAR_PATH"
  fi

  IFS=',' read -ra REGIONS <<< "$REGIONS_CSV"
  for REGION in "${REGIONS[@]}"; do
    REGION="$(echo "$REGION" | xargs)"
    REPO_NAME="${REPO_TEMPLATE//\$\{AWS::AccountId\}/$ACCOUNT_ID}"
    REPO_NAME="${REPO_NAME//\$\{AWS::Region\}/$REGION}"
    IMAGE_TAG="${TAG_TEMPLATE//\$\{AWS::AccountId\}/$ACCOUNT_ID}"
    IMAGE_TAG="${IMAGE_TAG//\$\{AWS::Region\}/$REGION}"
    # ECR's own answer for the registry host: the domain differs by partition (amazonaws.com,
    # amazonaws.com.cn, amazonaws.eu, ...), so it is never assembled here.
    REGISTRY="$(registry_host "$REGION")"
    FULL_IMAGE="$REGISTRY/$REPO_NAME:$IMAGE_TAG"

    echo
    echo "--- $REGION: $REPO_NAME:$IMAGE_TAG ---"

    ensure_repo "$REGION" "$REPO_NAME"

    if image_exists "$REGION" "$REPO_NAME" "$IMAGE_TAG"; then
      echo "  image tag $IMAGE_TAG already present — skipping push"
      continue
    fi

    if [ "$DRY_RUN" = "true" ]; then
      echo "  (dry-run: would crane push to $FULL_IMAGE)"
      continue
    fi

    # Log in to the region's ECR registry. crane stashes creds in
    # ~/.docker/config.json (same as docker), scoped to the registry
    # host, so subsequent pushes pick them up automatically. Token is
    # valid for 12 hours — plenty for a single deploy pipeline.
    aws ecr get-login-password --region "$REGION" \
      | crane auth login --username AWS --password-stdin "$REGISTRY"
    crane push "$UNCOMPRESSED" "$FULL_IMAGE"
    echo "  pushed $FULL_IMAGE"
  done

  rm -f "$UNCOMPRESSED"
done

echo
echo "All docker image assets pushed."
