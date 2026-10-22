#!/usr/bin/env bash
# npx projen update-template [--from <path-to-cre-demo-template>] [--allow-branch]
#
# Refresh this demo's copy of the CRE project-type package (projenrc/cre/) and its installed
# construct library (cre-constructs/, the `@cre/constructs` dependency; new and changed constructs
# arrive, removed ones go) from a cre-demo-template clone, then re-synth and build. The package is never edited inside a demo; a change goes to the
# template via an MR, and every demo picks it up with this task.
#
# Template location, first match wins: --from <path>, $CRE_TEMPLATE_DIR, the clone recorded on this
# machine in .cre/template.json (`templatePath`, its real path, written by attach-template.sh; older
# projects: projenrc/cre/SOURCE.json), or the
# sibling directory under the recorded `templateDirName` (the layout the skill links rely on). When
# none exists, it stops and says how to point it at one; the path used is written back.
#
# Before copying, it pulls the clone (`git pull --ff-only` from origin's default branch, recorded as
# `templateBranch`) when the clone is on that branch with no uncommitted changes to tracked files.
# A clone on another branch (template work in progress) is refused: its unmerged changes would land
# in this project. --allow-branch uses it as it is (testing a template change). A clone with local
# changes outside the package is used as it is, with a warning. The pull also updates the skills
# every project links from the clone.
#
# It prints the template's CHANGELOG entries added since the version this project had, and
# refreshes AGENTS.md and CLAUDE.md from the skeleton when the project hasn't edited them (an
# edited one is left alone, the template's new version written to .cre/template/<file> to compare).
#
# After copying, attach-template.sh rewrites the breadcrumb and re-links the vended skills from the
# template's current list, so a moved clone or a renamed, added or removed skill is picked up.
#
# Exit codes: 0 updated (or already current) | 10 refused, nothing changed | 20 synth/build failed
# after copying (the diff is left for you to inspect; the restore command is printed).
set -uo pipefail

die() { echo "❌ $2" >&2; exit "$1"; }

# The whole run is one function, parsed before anything executes, so pulling the clone (which may
# rewrite this very file when it is run from the clone) can't change the code mid-run.
main() {

FROM="" ALLOW_BRANCH="${CRE_TEMPLATE_ALLOW_BRANCH:-}"
while [ $# -gt 0 ]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --allow-branch) ALLOW_BRANCH=1; shift ;;
    *) die 10 "Unknown argument: $1" ;;
  esac
done

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die 10 "Run inside the demo's git repository."
cd "$ROOT"
DEST=projenrc/cre
CONSTRUCTS=cre-constructs
[ -f "$DEST/SOURCE.json" ] || die 10 "$DEST/SOURCE.json not found; this demo was not scaffolded with the CRE project-type package."

json_get() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"; }

if [ -z "$FROM" ]; then
  FROM="${CRE_TEMPLATE_DIR:-}"
fi
RECORDED=""
[ -f .cre/template.json ] && RECORDED=$(json_get .cre/template.json templatePath)
[ -n "$RECORDED" ] || RECORDED=$(json_get "$DEST/SOURCE.json" templatePath)
REMOTE=$(json_get "$DEST/SOURCE.json" templateRemote)
NAME=$(json_get "$DEST/SOURCE.json" templateDirName)
if [ -z "$FROM" ]; then
  if [ -n "$RECORDED" ] && [ -d "$RECORDED/packages/cre-projen/src" ]; then
    FROM="$RECORDED"
  else
    # The sibling the skill links point at, under the name recorded at scaffold time.
    FROM="../${NAME:-cre-demo-template}"
  fi
fi
if [ ! -d "$FROM/packages/cre-projen/src" ]; then
  die 10 "No cre-demo-template clone at '${RECORDED:-$FROM}' or '$FROM' (looked for packages/cre-projen/src). Point it at your clone once: npx projen update-template --from <path-to-cre-demo-template>${REMOTE:+ (no clone? git clone $REMOTE)}. The path is recorded for next time."
fi
FROM=$(cd "$FROM" && pwd -P)
PKG="$FROM/packages/cre-projen"

# Refuse to mix in uncommitted template work, or to overwrite uncommitted changes here.
if [ -n "$(git -C "$FROM" status --porcelain -- packages/cre-projen constructs/src)" ]; then
  die 10 "The template at $FROM has uncommitted changes under packages/cre-projen or constructs/src. Commit or stash them there first."
fi
for d in "$DEST" "$CONSTRUCTS"; do
  # SOURCE.json is this tool's own record (rewritten on every run), never a hand edit.
  if [ -n "$(git status --porcelain -- "$d" ":(exclude)$DEST/SOURCE.json")" ]; then
    die 10 "$d has uncommitted changes. It is installed from the template and never edited here; discard them (git restore --source=HEAD -- $d) or move the change to the template."
  fi
done
# Same template? A different origin is allowed (a fork), but say so.
ORIGIN=$(git -C "$FROM" remote get-url origin 2>/dev/null || true)
if [ -n "$REMOTE" ] && [ -n "$ORIGIN" ] && [ "$ORIGIN" != "$REMOTE" ]; then
  echo "⚠️  $FROM's origin is $ORIGIN, not $REMOTE (the template this project was made from). Updating from it anyway." >&2
fi
# Pull the clone to the latest template, unless someone is working in it (or this is the clone's
# own copy, re-run below, which comes after the pull).
TBRANCH=$(json_get "$DEST/SOURCE.json" templateBranch)
TBRANCH=${TBRANCH:-$(git -C "$FROM" symbolic-ref -q --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')}
TBRANCH=${TBRANCH:-main}
ON=$(git -C "$FROM" branch --show-current 2>/dev/null)
if [ -n "${CRE_UPDATE_REEXEC:-}" ]; then
  :
elif [ -z "$ORIGIN" ]; then
  echo "⚠️  The template clone has no origin; updating from it as it is." >&2
elif [ "$ON" != "$TBRANCH" ]; then
  [ -n "$ALLOW_BRANCH" ] || die 10 "The template clone ($FROM) is on branch '${ON:-detached HEAD}', not $TBRANCH: its unmerged changes would come into this project. Switch it back (git -C $FROM switch $TBRANCH), or pass --allow-branch to test that branch here."
  echo "⚠️  The template clone is on branch '${ON:-detached HEAD}', not $TBRANCH (--allow-branch): not pulling; updating from it as it is." >&2
elif [ -n "$(git -C "$FROM" status --porcelain --untracked-files=no)" ]; then
  echo "⚠️  The template clone has uncommitted changes: not pulling. Updating from it as it is." >&2
elif OUT=$(git -C "$FROM" pull -q --ff-only origin "$TBRANCH" 2>&1); then
  echo "Pulled the template clone ($FROM, $TBRANCH): now at $(git -C "$FROM" rev-parse --short HEAD)." >&2
else
  echo "⚠️  Could not pull the template clone (${OUT##*$'\n'}); updating from it as it is." >&2
fi

# Run the clone's own (just pulled) update-template, not this project's vendored copy: the newest
# logic does the copy, the breadcrumb and the skill links, even on the first update after a change
# to this script. The second run skips the pull and comes straight here.
SELF="$FROM/packages/cre-projen/scripts/template/update-template.sh"
if [ -z "${CRE_UPDATE_REEXEC:-}" ] && [ -f "$SELF" ]; then
  exec env CRE_UPDATE_REEXEC=1 CRE_TEMPLATE_ALLOW_BRANCH="$ALLOW_BRANCH" bash "$SELF" --from "$FROM"
fi

OLD_SHA=$(json_get "$DEST/SOURCE.json" templateSha)
OLD_VER=$(json_get "$DEST/SOURCE.json" packageVersion)
NEW_SHA=$(git -C "$FROM" rev-parse HEAD)
NEW_VER=$(json_get "$PKG/package.json" version)
if [ "$OLD_SHA" = "$NEW_SHA" ]; then
  # Same template commit, but the clone may have moved or the skill links gone stale.
  bash "$PKG/scripts/template/attach-template.sh" "$FROM" "$ROOT" || die 20 "Recording the template and linking its skills failed."
  echo "✅ Already at template ${NEW_SHA:0:9} (package $NEW_VER)." >&2
  exit 0
fi

for d in src scripts steering; do
  rsync -a --delete --exclude '__pycache__' --exclude '.pytest_cache' "$PKG/$d/" "$DEST/$d/"
done
bash "$PKG/scripts/template/sync-constructs.sh" "$FROM" "$CONSTRUCTS" || die 20 "Installing the construct library failed."
# AGENTS.md / CLAUDE.md: the skeleton's, refreshed where the project kept them as scaffolded.
python3 - "$FROM" "$OLD_SHA" "$ROOT" <<'PY' || die 20 "Refreshing AGENTS.md / CLAUDE.md failed."
import json, os, subprocess, sys
tpl, old_sha, root = sys.argv[1:4]
try:
    name = json.load(open(os.path.join(root, "cre-setup.json"))).get("project", "")
except (OSError, ValueError):
    name = ""
name = name or json.load(open(os.path.join(root, "package.json"))).get("name", "")
fill = lambda t: t.replace("__DEMO_NAME__", name)
for f in ("AGENTS.md", "CLAUDE.md"):
    src = os.path.join(tpl, "skeleton", "_shared", f)
    if not os.path.isfile(src):
        continue
    new = fill(open(src).read())
    cur_path = os.path.join(root, f)
    cur = open(cur_path).read() if os.path.isfile(cur_path) else None
    if cur == new:
        continue
    r = subprocess.run(["git", "-C", tpl, "show", f"{old_sha}:skeleton/_shared/{f}"], capture_output=True, text=True) if old_sha else None
    old = fill(r.stdout) if r is not None and r.returncode == 0 else None
    if cur is None or (old is not None and cur == old):
        open(cur_path, "w").write(new)
        print(f"Updated {f} from the template (it was as scaffolded).", file=sys.stderr)
    else:
        side = os.path.join(root, ".cre", "template", f)
        os.makedirs(os.path.dirname(side), exist_ok=True)
        open(side, "w").write(new)
        print(f"⚠️  {f} was edited in this project, so it is left as is. The template's version changed: compare with "
              f"diff {f} .cre/template/{f}   and merge what you want.", file=sys.stderr)
PY
# The breadcrumb (path, origin, branch, commit, version) and the skill links, from the clone used.
bash "$PKG/scripts/template/attach-template.sh" "$FROM" "$ROOT" || die 20 "Recording the template and linking its skills failed."

echo "Copied package $OLD_VER (${OLD_SHA:0:9}) → $NEW_VER (${NEW_SHA:0:9}). Re-synthesising and building..." >&2
RESTORE="git restore --source=HEAD --staged --worktree -- $DEST $CONSTRUCTS .projen AGENTS.md CLAUDE.md && git clean -fdq -- $DEST $CONSTRUCTS && npx projen"
LOG="$(git rev-parse --git-common-dir)/cre-update-template.log"
if ! npx projen >"$LOG" 2>&1 || ! npx projen build >>"$LOG" 2>&1; then
  tail -30 "$LOG" >&2
  die 20 "Synth or build failed on the new version (log: $LOG). Inspect with git diff, or go back with:
   $RESTORE"
fi
git diff --stat -- . >&2
# What changed in the template, in its own words: the CHANGELOG lines added since this project's version.
if [ -n "$OLD_SHA" ] && git -C "$FROM" cat-file -e "$OLD_SHA^{commit}" 2>/dev/null; then
  NOTES=$(git -C "$FROM" diff --unified=0 "$OLD_SHA" "$NEW_SHA" -- CHANGELOG.md 2>/dev/null | sed -n 's/^+\([^+]\)/\1/p; s/^+$//p')
  if [ -n "$NOTES" ]; then
    echo "" >&2
    echo "What changed in the template (CHANGELOG.md, $OLD_VER → $NEW_VER):" >&2
    echo "$NOTES" | head -80 | sed 's/^/   /' >&2
    [ "$(echo "$NOTES" | wc -l)" -le 80 ] || echo "   … (more: git -C $FROM diff ${OLD_SHA:0:9} ${NEW_SHA:0:9} -- CHANGELOG.md)" >&2
  fi
fi
echo "✅ Updated to template ${NEW_SHA:0:9}. Review the diff, then push it with npx projen push." >&2
}

main "$@"
