#!/usr/bin/env bash
# Report whether this demo's vended skills actually resolve.
#
# The skills in .kiro/skills/ are SYMLINKS into the sibling cre-demo-template clone, so a pull of
# that clone (update-template does one) updates every project at once. The cost of that is a failure
# mode with no error message: if the template is renamed, moved, or absent (a fresh clone of the
# project on its own), every link dangles, `git status` stays clean, and the skills simply never
# appear in the harness. Nothing tells you why /pdd stopped existing. This script tells you.
#
# Bundled with the cre-projen package (projenrc/cre/scripts/build/) and invoked by `npx projen skills:doctor` rather than being inlined as a task
# `exec` string: projen's dax shell does not expand braced ${VAR} and the `bash -c` escape hatch
# forbids single quotes in its payload (MUST-HONOR rules 14/15). A real script file has neither
# constraint.
set -uo pipefail   # deliberately NOT -e: we want to report every link, not stop at the first bad one

SKILLS_DIR=".kiro/skills"
FAIL=0

printf 'Vended skills (%s)\n' "$SKILLS_DIR"

if [ ! -d "$SKILLS_DIR" ]; then
  echo "  ERROR  no $SKILLS_DIR directory — was this demo scaffolded by cre-demo-init?"
  exit 1
fi

shopt -s nullglob
ENTRIES=("$SKILLS_DIR"/*)
if [ ${#ENTRIES[@]} -eq 0 ]; then
  echo "  ERROR  $SKILLS_DIR is empty"
  exit 1
fi

for ENTRY in "${ENTRIES[@]}"; do
  NAME=$(basename "$ENTRY")
  if [ -L "$ENTRY" ]; then
    TARGET=$(readlink "$ENTRY")
    if [ -e "$ENTRY" ]; then
      printf '  ok        %-14s -> %s\n' "$NAME" "$TARGET"
    else
      printf '  DANGLING  %-14s -> %s\n' "$NAME" "$TARGET"
      FAIL=1
    fi
  elif [ -d "$ENTRY" ]; then
    # A real directory: either a demo-local skill (fine) or a stale pre-symlink copy (not).
    printf '  copy      %-14s (real directory, not a link to the template)\n' "$NAME"
  else
    printf '  ?         %-14s (neither a symlink nor a directory)\n' "$NAME"
    FAIL=1
  fi
done

# The harness hop: Claude Code reads .claude/skills, Kiro reads .kiro/skills. One link joins them,
# so a broken hop here hides every skill from Claude Code while Kiro still sees them all.
printf '\nHarness hop (.claude/skills)\n'
if [ -L .claude/skills ]; then
  if [ -e .claude/skills ]; then
    printf '  ok        -> %s\n' "$(readlink .claude/skills)"
  else
    printf '  DANGLING  -> %s\n' "$(readlink .claude/skills)"
    FAIL=1
  fi
elif [ -f .claude/skills ]; then
  echo "  ERROR  .claude/skills is a FILE containing a path, not a symlink."
  echo "         This is the Windows checkout failure: git config core.symlinks true, then re-checkout."
  FAIL=1
elif [ -d .claude/skills ]; then
  echo "  copy   .claude/skills is a real directory (not a link to .kiro/skills)"
else
  echo "  ERROR  .claude/skills missing — Claude Code will find no skills in this repo"
  FAIL=1
fi

if [ "$FAIL" -ne 0 ]; then
  cat <<'EOS'

To repair: the links are relative and expect the cre-demo-template clone to be a SIBLING of this
project, under the directory name recorded in projenrc/cre/SOURCE.json (templateDirName):

  <parent>/
    cre-demo-template/     <- the clone
    <this-project>/

Put the clone back there (or clone it there), then re-link every vended skill from it:

  npx projen update-template                       # finds the recorded or sibling clone
  npx projen update-template --from <your-clone>   # or name the clone explicitly

If you do not have the template and only need to build/deploy, ignore this: skills are agent
tooling, and nothing in the build or the CI pipeline reads .kiro/.
EOS
  exit 1
fi

printf '\nAll vended skills resolve.\n'
