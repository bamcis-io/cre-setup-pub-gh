#!/usr/bin/env bash
# sync-constructs.sh <cre-demo-template dir> <dest dir>
#
# Install the template's construct library into a project as if it came from npm: the whole of
# <template>/constructs/src/ is mirrored into <dest> (default layout: cre-constructs/ at the project
# root), with a package.json naming it `@cre/constructs`, and the project depends on it as
# `link:./cre-constructs`. Code imports it like any package:
#
#     import { RegionalNetwork, LoadGenerator } from '@cre/constructs';
#
# It is a dependency, not project source: never edit it in a project. Changes go to
# cre-demo-template (constructs/), and `npx projen update-template` re-runs this script. Files the
# template no longer has are deleted (rsync --delete). A manifest of file hashes (.cre-manifest) is
# written so `verify-constructs.sh` can fail the build when the copy was edited by hand.
#
# Used by recipe STEP 4 (scaffold) and scripts/template/update-template.sh.
set -euo pipefail

TEMPLATE="${1:?usage: sync-constructs.sh <template-dir> <dest-dir>}"
DEST="${2:?usage: sync-constructs.sh <template-dir> <dest-dir>}"
SRC="$TEMPLATE/constructs/src"
[ -d "$SRC" ] || { echo "❌ No construct library at $SRC" >&2; exit 10; }

mkdir -p "$DEST"
rsync -a --delete \
  --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
  --exclude '.DS_Store' --exclude 'Thumbs.db' --exclude '*.swp' \
  --exclude 'package.json' --exclude '.cre-manifest' \
  "$SRC/" "$DEST/"

VERSION=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$TEMPLATE/constructs/package.json")
SHA=$(git -C "$TEMPLATE" rev-parse HEAD 2>/dev/null || echo unknown)
python3 - "$DEST/package.json" "$VERSION" "$SHA" <<'PY'
import json, sys
path, version, sha = sys.argv[1:4]
json.dump({
    "name": "@cre/constructs",
    "version": version,
    "private": True,
    "description": "CRE construct library, installed from cre-demo-template/constructs/src. Do not edit: "
                   "change it in cre-demo-template and run `npx projen update-template`.",
    "main": "index.ts",
    "types": "index.ts",
    "creTemplateSha": sha,
}, open(path, "w"), indent=2)
open(path, "a").write("\n")
PY

bash "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/verify-constructs.sh" --write "$DEST"
echo "✅ @cre/constructs $VERSION (template ${SHA:0:9}) → $DEST" >&2
