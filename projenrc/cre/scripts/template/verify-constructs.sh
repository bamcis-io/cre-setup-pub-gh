#!/usr/bin/env bash
# verify-constructs.sh [--write] [dir]      (default dir: cre-constructs)
#
# The installed construct library (`@cre/constructs`) is a dependency, like a package in
# node_modules: it must be byte-identical to what `sync-constructs.sh` installed. This checks every
# file against the hash manifest (.cre-manifest) written at install time, and fails (exit 1) on a
# changed, added or removed file, so a hand edit can't slip into a project and silently diverge
# from cre-demo-template. --write regenerates the manifest (only sync-constructs.sh does that).
set -euo pipefail

WRITE=""
[ "${1:-}" = --write ] && { WRITE=1; shift; }
DIR="${1:-cre-constructs}"
[ -d "$DIR" ] || { echo "❌ $DIR not found: the construct library isn't installed. Run: npx projen update-template" >&2; exit 1; }

# sha256sum (coreutils/busybox: Linux CI images) or shasum (macOS); both print "<hash>  ./path".
if command -v sha256sum >/dev/null 2>&1; then SHA256=(sha256sum); else SHA256=(shasum -a 256); fi
manifest() {
  # Files the OS, editors and tools drop in any directory (Finder's .DS_Store, pytest's cache,
  # bytecode) aren't edits: ignored here and never installed (sync-constructs.sh excludes the same).
  (cd "$DIR" && find . -type f ! -name .cre-manifest ! -path '*/__pycache__/*' ! -name '*.pyc' \
    ! -name .DS_Store ! -name 'Thumbs.db' ! -name '*.swp' ! -path '*/.pytest_cache/*' -print0 |
    LC_ALL=C sort -z | xargs -0 "${SHA256[@]}")
}

if [ -n "$WRITE" ]; then
  manifest >"$DIR/.cre-manifest"
  exit 0
fi

[ -f "$DIR/.cre-manifest" ] || { echo "❌ $DIR/.cre-manifest missing. Reinstall with: npx projen update-template" >&2; exit 1; }
CURRENT=$(manifest) || { echo "❌ Could not hash $DIR (${SHA256[*]} failed)." >&2; exit 1; }
if ! DIFF=$(diff "$DIR/.cre-manifest" <(printf '%s\n' "$CURRENT")); then
  echo "❌ $DIR (@cre/constructs) differs from what cre-demo-template installed:" >&2
  # Normal (GNU/BSD: "< h  ./p") and unified (busybox: "-h  ./p") diff lines both carry the path.
  echo "$DIFF" | sed -n 's/^[-+<>] *[0-9a-f]\{64\}  \.\//  /p' | sort -u >&2
  echo "   It is a dependency: never edit it here. Make the change in cre-demo-template/constructs," >&2
  echo "   then run npx projen update-template, which reinstalls it as the template has it (an added" >&2
  echo "   file is removed too). To discard only tracked edits: git restore -- $DIR" >&2
  exit 1
fi
echo "✅ $DIR matches its install manifest."
