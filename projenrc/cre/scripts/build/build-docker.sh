#!/usr/bin/env sh
#
# Build every DockerImageAsset listed in dist/container-plan.tsv with
# Kaniko and save each one to assets/containers/<hash>.tar.gz.
#
# Kaniko is used instead of `docker build` because our CI runner fleet
# (shared AWS corporate GitLab) doesn't allow Docker-in-Docker and
# blocks the CLONE_NEWUSER syscall that rootless podman/buildah need.
# Kaniko builds OCI images in a chroot — no daemon, no user namespaces.
#
# This script is written for the kaniko `:debug` image, which is
# distroless + busybox — so no python, no apk, no bash. Everything here
# is POSIX sh + busybox coreutils. The JSON-heavy work (enumerating CDK
# assets, writing manifest.json, computing regions) lives in
# build/container-plan.sh, which runs on the plain node image and
# produces dist/container-plan.tsv for this script to consume.
#
# Output layout (relative to repo root):
#
#   assets/containers/<hash>.tar.gz
#
# Container_Manifest (assets/containers/manifest.json) is produced by
# build/container-plan.sh, not here. The package:content task zips
# assets/, so the containers ride along in dist/content.zip.
#
# Invocation paths:
#   1. Native kaniko (CI job running on the kaniko image):
#        /kaniko/executor --no-push --tarPath=<file>
#   2. Docker-wrapped kaniko (local dev with docker daemon):
#        docker run --rm $KANIKO_IMAGE ...
# Both produce identical OCI tarballs.
#
# Optional env:
#   KANIKO_IMAGE         Defaults to gcr.io/kaniko-project/executor:v1.23.2-debug.
#   CONTAINER_PLAN_FILE  Defaults to dist/container-plan.tsv.
#   CONTAINERS_DIR       Defaults to assets/containers.
#   CDK_OUT_DIR          Defaults to cdk.out (the staged asset directories
#                        referenced by relative paths in the plan file).
#   CONTAINER_CACHE_DIR  Persistent content-addressed tarball cache. Defaults to
#                        dist/.cache/containers.
#
# CACHING: CDK image hashes are content hashes of the build context, so an existing
# <hash>.tar.gz is byte-identical to what a rebuild would produce. This script keeps a
# persistent cache under dist/.cache (which `pre-compile` does NOT wipe, unlike assets/ +
# cdk.out/) and, on a cache hit, reuses the tarball and SKIPS kaniko entirely — no base-image
# pull (so no public.ecr.aws rate-limit hit) and no recompile. Result: an unchanged
# `build:docker` is a ~2s no-op instead of a multi-minute full rebuild. A real content change
# flips that image's hash → cache miss → one rebuild → re-cached. In CI, the build:docker job's
# GitLab `cache:` block persists dist/.cache/containers across pipelines (keyed per branch).

set -eu

KANIKO_IMAGE="${KANIKO_IMAGE:-gcr.io/kaniko-project/executor:v1.23.2-debug}"
CONTAINER_PLAN_FILE="${CONTAINER_PLAN_FILE:-dist/container-plan.tsv}"
CONTAINERS_DIR="${CONTAINERS_DIR:-assets/containers}"
CDK_OUT_DIR="${CDK_OUT_DIR:-cdk.out}"
CONTAINER_CACHE_DIR="${CONTAINER_CACHE_DIR:-dist/.cache/containers}"

mkdir -p "$CONTAINERS_DIR"

# ---- preflight ------------------------------------------------------------

if [ ! -f "$CONTAINER_PLAN_FILE" ]; then
  echo "ERROR: container plan file not found: $CONTAINER_PLAN_FILE" >&2
  echo "  (did build:container-plan / yarn ci:build:container-plan run?)" >&2
  exit 1
fi

# Count non-empty lines — gives us "0 images" detection without any jq/python.
COUNT=$(grep -cv '^[[:space:]]*$' "$CONTAINER_PLAN_FILE" || true)
if [ "$COUNT" = "0" ]; then
  echo "Container plan is empty — nothing to build."
  exit 0
fi

# ---- pick a kaniko invocation ---------------------------------------------

# Native kaniko (CI): the job image IS kaniko, the executor binary is
# at /kaniko/executor. No container wrapping needed.
if [ -x /kaniko/executor ]; then
  echo "Using native kaniko at /kaniko/executor"
  kaniko_mode="native"
# Docker-wrapped kaniko (local dev): the host has docker, so launch
# the kaniko container with the build context bind-mounted.
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "Using docker-wrapped kaniko: $KANIKO_IMAGE"
  kaniko_mode="docker"
elif [ -z "${CI:-}" ] && [ -z "${CRE_REQUIRE_IMAGES:-}" ]; then
  # A local build (npx projen build, and the validation `commit` / `push` run) on a machine with no
  # Docker: everything else is still checked, and CI builds the images (#33). A local deploy of
  # this build can't push images; deploy:docker then names the missing tarballs.
  echo "⚠  No Docker here (and no /kaniko/executor): the container images are NOT built locally. CI builds them." >&2
  echo "   To build them here, start Docker; CRE_REQUIRE_IMAGES=1 makes this an error." >&2
  exit 0
else
  echo "Neither /kaniko/executor nor docker are available — cannot build images." >&2
  echo "  (on CI, build:docker runs on the kaniko image; locally, install Docker)" >&2
  exit 1
fi

echo "Building $COUNT image(s) from $CONTAINER_PLAN_FILE into $CONTAINERS_DIR"

REPO_ROOT="$(pwd)"

# ---- build loop -----------------------------------------------------------

# Read tab-separated plan file line by line. Busybox `read -r` splits on
# IFS; we set IFS to tab explicitly so hashes containing no tabs are
# safe and the third field captures the platform unchanged.
while IFS="$(printf '\t')" read -r IMAGE_HASH DIRECTORY PLATFORM; do
  # Skip blank lines defensively — they shouldn't exist but grep -v handled
  # counting and we'd rather survive stray whitespace than fail loudly here.
  [ -z "${IMAGE_HASH:-}" ] && continue

  FINAL_TARGZ="$CONTAINERS_DIR/${IMAGE_HASH}.tar.gz"
  CACHED_TARGZ="$CONTAINER_CACHE_DIR/${IMAGE_HASH}.tar.gz"

  # Cache hit: an identical image (same content hash) was already built — reuse it and skip
  # kaniko entirely (this is what makes an unchanged build a near no-op). Checked BEFORE the
  # staged-dir check because a cache hit needs neither cdk.out nor a base-image pull.
  if [ -f "$CACHED_TARGZ" ]; then
    echo
    echo "=== $IMAGE_HASH === (cache hit — reusing $CACHED_TARGZ, no rebuild)"
    cp "$CACHED_TARGZ" "$FINAL_TARGZ"
    continue
  fi

  STAGED_DIR="$CDK_OUT_DIR/$DIRECTORY"
  if [ ! -d "$STAGED_DIR" ]; then
    echo "ERROR: staged directory not found: $STAGED_DIR" >&2
    exit 1
  fi

  TAR_PATH="$CONTAINERS_DIR/${IMAGE_HASH}.tar"

  echo
  echo "=== $IMAGE_HASH ==="
  echo "  source:   $STAGED_DIR"
  echo "  platform: $PLATFORM"
  echo "  tar:      ${TAR_PATH}.gz"

  # --no-push: we're just producing a tarball, not touching a registry
  # --tarPath: emit docker-archive format for crane/skopeo/docker-load consumption
  # --destination: required even with --no-push, used as the image's
  #                canonical reference inside the tarball
  # --custom-platform: honor the platform from the CDK asset (arm64
  #                    for our Locust container).
  # --cleanup: kaniko unpacks each base image over the container's own root filesystem, so
  #            without it the next image in this loop starts on the previous one's files. In
  #            the docker-wrapped mode every build is a fresh container; harmless there.
  if [ "$kaniko_mode" = "native" ]; then
    /kaniko/executor \
      --dockerfile="$REPO_ROOT/$STAGED_DIR/Dockerfile" \
      --context="$REPO_ROOT/$STAGED_DIR" \
      --destination="cdk-asset:$IMAGE_HASH" \
      --tarPath="$REPO_ROOT/$TAR_PATH" \
      --custom-platform="$PLATFORM" \
      --no-push \
      --cleanup
  else
    # Docker-wrapped: mount the repo root so kaniko sees both the
    # context and the output directory.
    docker run --rm \
      -v "$REPO_ROOT:/workspace" \
      "$KANIKO_IMAGE" \
      --dockerfile="/workspace/$STAGED_DIR/Dockerfile" \
      --context="/workspace/$STAGED_DIR" \
      --destination="cdk-asset:$IMAGE_HASH" \
      --tarPath="/workspace/$TAR_PATH" \
      --custom-platform="$PLATFORM" \
      --no-push \
      --cleanup
  fi

  # Kaniko emits an uncompressed tar; compress to match the .tar.gz
  # convention the manifest advertises.
  gzip -f "$TAR_PATH"

  # Populate the content-addressed cache so the next build (local or, via GitLab cache, CI)
  # reuses this exact image instead of pulling + rebuilding.
  mkdir -p "$CONTAINER_CACHE_DIR"
  cp "$FINAL_TARGZ" "$CACHED_TARGZ"
done < "$CONTAINER_PLAN_FILE"

echo
echo "Built $COUNT image(s)."
