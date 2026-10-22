#!/usr/bin/env bash
# attach-template.sh <template-clone> <project-dir>
#
# Ties a CRE project to the cre-demo-template clone it is built from:
#   1. The breadcrumb, projenrc/cre/SOURCE.json (committed): the clone's
#      directory name, its origin URL (`templateRemote`), its default branch (`templateBranch`), the
#      commit and the package version; and .cre/template.json (gitignored, per machine): the
#      clone's real absolute path (`templatePath`). `npx projen update-template` reads both to find
#      the clone and pull it. Other keys are kept.
#   2. The vended skills: .kiro/skills/<name> links to the clone's skills/<name> for each name in the
#      template's scripts/template/vended-skills.txt, so a skill the template adds, renames or drops
#      reaches the project on its next update. The links are relative and committed; with the
#      template as a sibling of the project (the layout the recipe makes) they are
#      `../../../<template-dir>/skills/<name>` and work on any machine with the same layout. A link
#      into the template's skills/ that is no longer vended is removed; anything else in .kiro/skills
#      (the project's own skills) is never touched. .claude/skills -> .kiro/skills for Claude Code.
#   3. GitLab projects: .cre/gitlab-token, a link to the token file the template records.
#
# Run by recipe STEP 3 at scaffold time and by update-template on every update. Idempotent.
# Exit codes: 0 done | 10 refused (not a template clone, not a project directory).
set -uo pipefail
die() { echo "❌ $2" >&2; exit "$1"; }

[ $# -eq 2 ] || die 10 "usage: attach-template.sh <template-clone> <project-dir>"
TPL=$(cd "$1" 2>/dev/null && pwd -P) || die 10 "No directory at $1."
PROJ=$(cd "$2" 2>/dev/null && pwd -P) || die 10 "No directory at $2."
PKG="$TPL/packages/cre-projen"
[ -d "$PKG/src" ] && [ -d "$TPL/skills" ] || die 10 "$TPL is not a cre-demo-template clone (no packages/cre-projen/src or skills/)."
case "$PROJ/" in "$TPL"/*) die 10 "The project $PROJ is inside the template clone; it must be its own directory." ;; esac
[ "$(dirname "$TPL")" = "$(dirname "$PROJ")" ] ||
  echo "⚠️  The template clone ($TPL) is not a sibling of the project ($PROJ): the skill links work only on machines with this same layout." >&2

# ---- 1. breadcrumb --------------------------------------------------------------------------------
REMOTE=$(git -C "$TPL" remote get-url origin 2>/dev/null || true)
# The branch origin/HEAD names (what a pull tracks), else main.
BRANCH=$(git -C "$TPL" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')
BRANCH=${BRANCH:-main}
SHA=$(git -C "$TPL" rev-parse HEAD 2>/dev/null) || die 10 "$TPL is not a git repository."
VER=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$PKG/package.json")
mkdir -p "$PROJ/projenrc/cre"
mkdir -p "$PROJ/.cre"
python3 - "$PROJ/projenrc/cre/SOURCE.json" "$TPL" "$REMOTE" "$BRANCH" "$SHA" "$VER" "$PROJ/.cre/template.json" <<'PY'
import json, os, sys
path, tpath, remote, branch, sha, ver, local = sys.argv[1:8]
d = json.load(open(path)) if os.path.exists(path) else {}
# The clone's absolute path differs per machine: it goes in the gitignored .cre/template.json, not
# the committed SOURCE.json (where it churned between teammates; #35).
d.pop("templatePath", None)
d.update({"templateDirName": os.path.basename(tpath), "templateRemote": remote,
          "templateBranch": branch, "templateSha": sha, "packageVersion": ver})
with open(path, "w") as f:
    json.dump(d, f, indent=2)
    f.write("\n")
with open(local, "w") as f:
    json.dump({"templatePath": tpath}, f, indent=2)
    f.write("\n")
PY

# ---- 2. skills ------------------------------------------------------------------------------------
SKILLS="$PROJ/.kiro/skills"
mkdir -p "$SKILLS"
WANT=$(grep -v '^[[:space:]]*\(#\|$\)' "$PKG/scripts/template/vended-skills.txt")
REL=$(python3 -c 'import os,sys;print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$TPL/skills" "$SKILLS")
for S in $WANT; do
  [ -d "$TPL/skills/$S" ] || { echo "⚠️  The template has no skills/$S; not linked." >&2; continue; }
  if [ -e "$SKILLS/$S" ] && [ ! -L "$SKILLS/$S" ]; then
    echo "⚠️  .kiro/skills/$S is a real directory (the project's own skill?); left as is, not linked." >&2
    continue
  fi
  ln -sfn "$REL/$S" "$SKILLS/$S"
done
# Links into THIS template's skills/ that are no longer vended (removed from the list, or gl-push /
# gh-push from an older scaffold). Any other link is the project's own and is left alone.
for L in "$SKILLS"/*; do
  [ -L "$L" ] || continue
  N=$(basename "$L")
  echo "$WANT" | grep -qx "$N" && continue
  T=$(readlink "$L")
  R=$(cd "$SKILLS" && cd "$T" 2>/dev/null && pwd -P || true)
  if [ "${T#"$REL"/}" != "$T" ] || [ "${R#"$TPL"/skills/}" != "$R" ]; then
    rm -f "$L"
    echo "ℹ️  Unlinked .kiro/skills/$N (the template no longer vends it)." >&2
  fi
done
mkdir -p "$PROJ/.claude"
if [ ! -e "$PROJ/.claude/skills" ] || [ -L "$PROJ/.claude/skills" ]; then
  ln -sfn ../.kiro/skills "$PROJ/.claude/skills"
fi

# ---- 3. GitLab token link -------------------------------------------------------------------------
# The template records where this machine's GitLab API token file is: the link .agents/gitlab-token
# (gitignored), made by new_project.py from /cre-demo-init's answer. A GitLab project gets the same
# link at .cre/gitlab-token (gitignored), which its setup tasks and git tasks read. Only the path is
# shared; the token is never copied. A real file at .cre/gitlab-token is left alone.
REC="${CRE_TOKEN_RECORD:-$TPL/.agents/gitlab-token}"
PROVIDER=$(python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get("provider", ""))
except Exception: print("")' "$PROJ/cre-setup.json")
if [ "$PROVIDER" = gitlab ]; then
  LINK="$PROJ/.cre/gitlab-token"
  if [ ! -L "$REC" ]; then
    [ -L "$LINK" ] || echo "⚠  No GitLab token file is recorded in the template ($REC), so .cre/gitlab-token isn't linked. The git tasks then can't check MR state. Link it: ln -s <your token file> $LINK" >&2
  elif [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
    echo "⚠  $LINK is a real file, not a link to the token file; left as is." >&2
  elif [ "$(readlink "$LINK" 2>/dev/null)" != "$(readlink "$REC")" ]; then
    mkdir -p "$PROJ/.cre"
    ln -sfn "$(readlink "$REC")" "$LINK"
    echo "ℹ  Linked .cre/gitlab-token → $(readlink "$REC") (the token file the template records)." >&2
  fi
fi

echo "✅ Attached to the template clone at $TPL (${SHA:0:9}; origin ${REMOTE:-none}; branch $BRANCH)." >&2
