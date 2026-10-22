#!/usr/bin/env bash
#
# Container build plan generator.
#
# Reads every `cdk.out/*.assets.json`, enumerates unique DockerImageAssets,
# and emits two artifacts:
#
#   1. assets/containers/manifest.json  — Container_Manifest consumed by
#      build/deploy-docker.sh. Exact schema the design specifies:
#
#          [
#            {
#              "hash": "<content-hash>",
#              "platform": "linux/arm64",
#              "repository": "cdk-…-${AWS::AccountId}-${AWS::Region}",
#              "tag": "<content-hash>",
#              "stacks": ["<stack>", …]      (the stacks whose templates use it)
#            }
#          ]
#
#   2. dist/container-plan.tsv  — a tab-separated file consumed by
#      build/build-docker.sh on the kaniko image. Each line is:
#
#          <image-hash>\t<staged-directory>\t<platform>
#
#      The build script walks the file, runs /kaniko/executor once per
#      row, and is dependency-free (busybox sh + kaniko only — no python,
#      no apk). The kaniko `:debug` image is distroless + busybox, so a
#      python-free consumer is the only way to make build:docker run on
#      it out-of-the-box.
#
# This script runs on the plain node image (Build_Container_Plan_Job),
# where python3 is available. `build:container-plan` is registered as a
# granular projen task; `ci:build:container-plan` is the composite name
# the GitLab job invokes.
#
# Which regions each image is pushed to is decided at deploy time from the stacks that use it
# (deploy-docker.sh --stack-regions, generated from the stacks' regions); the build doesn't know.
#
# Optional env:
#   CDK_OUT_DIR          Defaults to cdk.out.

set -euo pipefail

CDK_OUT_DIR="${CDK_OUT_DIR:-cdk.out}"
CONTAINERS_DIR="assets/containers"
PLAN_FILE="dist/container-plan.tsv"

mkdir -p "$CONTAINERS_DIR" "$(dirname "$PLAN_FILE")"

if [ ! -d "$CDK_OUT_DIR" ]; then
  echo "ERROR: CDK_OUT_DIR not found: $CDK_OUT_DIR" >&2
  exit 1
fi

# ---- enumerate + write artifacts ------------------------------------------

# Single python invocation writes both files so the parse happens once.
# Reads every cdk.out/*.assets.json, dedups by image hash, outputs the
# manifest JSON and the TSV plan file side-by-side.
CDK_OUT_DIR="$CDK_OUT_DIR" \
MANIFEST_PATH="$CONTAINERS_DIR/manifest.json" \
PLAN_PATH="$PLAN_FILE" \
python3 - <<'PY'
import json
import os
import pathlib
import sys

cdk_out = pathlib.Path(os.environ["CDK_OUT_DIR"])
manifest_path = pathlib.Path(os.environ["MANIFEST_PATH"])
plan_path = pathlib.Path(os.environ["PLAN_PATH"])

seen = {}
entries = []
for assets_json in sorted(cdk_out.glob("*.assets.json")):
    with assets_json.open() as fh:
        manifest = json.load(fh)
    # <stack>.assets.json: the stack whose template uses these images. deploy-docker.sh pushes
    # each image to the regions of the stacks that use it (each stack's region prop says where it goes).
    stack = assets_json.name[: -len(".assets.json")]
    for image_hash, entry in (manifest.get("dockerImages") or {}).items():
        if image_hash in seen:
            if stack not in seen[image_hash]["stacks"]:
                seen[image_hash]["stacks"].append(stack)
            continue
        src = entry["source"]
        dest = next(iter(entry["destinations"].values()))
        e = {
            "hash": image_hash,
            "directory": src["directory"],
            "platform": src.get("platform", "linux/amd64"),
            "repository": dest["repositoryName"],
            "tag": dest["imageTag"],
            "stacks": [stack],
        }
        seen[image_hash] = e
        entries.append(e)

# manifest.json (deploy-side): no `directory`; `stacks` decides the push regions.
manifest_entries = [
    {
        "hash": e["hash"],
        "platform": e["platform"],
        "repository": e["repository"],
        "tag": e["tag"],
        "stacks": e["stacks"],
    }
    for e in entries
]
manifest_path.write_text(json.dumps(manifest_entries, indent=2) + "\n")

# container-plan.tsv (build-side) — exactly what build-docker.sh needs.
# Tabs as separators; one line per image. Trailing newline for sanity.
with plan_path.open("w") as fh:
    for e in entries:
        fh.write(f"{e['hash']}\t{e['directory']}\t{e['platform']}\n")

print(f"Wrote {len(entries)} image(s) to {manifest_path} and {plan_path}")
if not entries:
    print("ERROR: no DockerImageAssets found in cdk.out — check build:cdk artifacts", file=sys.stderr)
    sys.exit(1)
PY
