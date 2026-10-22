#!/usr/bin/env bash
# test:clean-purity — assert src/clean reads as an ordinary system: nothing in it may point at the
# failure, the incident or source it was modelled on, or the fact that it is a benchmark scenario.
# The review agent is only allowed to read src/clean, so this is the whole of what it sees.
#
# Generalized from request-router-metadata-app/tools/clean-purity-scan.sh. Two lists:
#   - GENERIC, below: scenario, injection and editorializing vocabulary that is wrong in any clean
#     tree. Case-insensitive extended regexes. Words with ordinary engineering meanings ("fault",
#     "retry", "inject" alone, as in dependency injection) are deliberately not on it; only
#     combinations like "inject failure" are.
#   - CRE_FORBIDDEN_TERMS: the scenario's own terms (the source system's names, incident ids,
#     commit SHAs, ...), from the project's `forbiddenTerms` option, newline-separated.
# Also scans file and directory NAMES, which a reviewer sees in every listing.
set -uo pipefail

CLEAN_DIR="${CLEAN_DIR:-src/clean}"
[ -d "$CLEAN_DIR" ] || { echo "clean-purity: $CLEAN_DIR not found" >&2; exit 1; }

GENERIC=(
  # The project's own nature
  '\bfailure[ _-]?scenario' '\bground[ _-]?truth' '\bred[ _-]?herring' '\bbenchmark' '\banswer key'
  '\bthe (intended|designed|deliberate|planted) (bug|failure|flaw|weakness)'
  '\b(intentional|deliberate|planted|seeded) (bug|failure|flaw|weakness|defect)'
  # Failure injection and its tooling (all of it belongs in src/deploy)
  '\binject(ion|ed|or|s)?[ _-]?(failure|fault|error|latency)' '\bfailure[ _-]?inject' '\bfault[ _-]?inject'
  '\berror[ _-]?rate[ _-]?(knob|param)' '\bchaos\b' '\bFIS\b' '\bFisNetworkExperiments\b'
  '\bFailureInjectionParameter\b' '\bInjectApi\b' '\blocust' '\bLoadGenerator\b'
  # Provenance: where this came from
  '\bCOE[ -]?[0-9]' '\bpost[ -]?mortem' '\bincident\b' '\brecreat(e|ion|ed|ing)\b' '\breproduc(e|tion) of\b'
  '\bmodell?ed (on|after)\b' '\bported (from|verbatim)\b' '\bfaithful' '\bpre-incident\b'
  '\bcre-demo-template\b' '\bfive-nines' '\bmulti-az-(workshop|observability)\b' '\breinvent-agents-workshop\b'
  # Editorializing the failure mechanism
  '\bmetastab' '\bbimodal\b' '\bretry storm' '\bthundering herd' '\bbrown[ -]?out' '\bcascad(e|ing) failure'
)

EXTRA=()
while IFS= read -r t; do [ -n "$t" ] && EXTRA+=("$t"); done <<<"${CRE_FORBIDDEN_TERMS:-}"

ARGS=(-rInE --exclude-dir=node_modules --exclude-dir=target --exclude-dir=__pycache__ --exclude-dir=.venv
  --exclude-dir=cdk.out --exclude-dir=dist --binary-files=without-match)

FAIL=0
for pat in "${GENERIC[@]}" ${EXTRA[@]+"${EXTRA[@]}"}; do
  if HITS=$(grep "${ARGS[@]}" -i -e "$pat" "$CLEAN_DIR" 2>/dev/null) && [ -n "$HITS" ]; then
    echo "CLEAN-PURITY: '$pat' found in $CLEAN_DIR:"
    echo "$HITS" | sed 's/^/    /'
    FAIL=1
  fi
  if NAMES=$(find "$CLEAN_DIR" -not -path '*/node_modules/*' | sed "s#^$CLEAN_DIR/##" | grep -iE -e "$pat") && [ -n "$NAMES" ]; then
    echo "CLEAN-PURITY: '$pat' in a file/directory name under $CLEAN_DIR:"
    echo "$NAMES" | sed 's/^/    /'
    FAIL=1
  fi
done

if [ "$FAIL" -ne 0 ]; then
  echo "clean-purity FAILED: $CLEAN_DIR must read as an ordinary system. Move failure tooling to src/deploy and reword the rest."
  exit 1
fi
echo "clean-purity passed: $CLEAN_DIR is clean (${#GENERIC[@]} generic + ${#EXTRA[@]} scenario terms)."
