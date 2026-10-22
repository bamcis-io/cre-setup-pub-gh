---
inclusion: always
---
# This project is a Failure Scenario

**Purpose:** a working system with an intentional failure built in, used as a benchmark to rate
how well a resilience agent finds it. The scenario can come from anywhere: the user's own
description, an AWS COE, a public post-mortem, a blog post. Whatever it is, the fidelity rules apply.

## `src/clean` — the system under review

The review agent is told to review **only `src/clean`**, and is not allowed to look outside it.

- It must read as an ordinary, working system. No identifiers, comments, file or directory names,
  docs, or test names that point at the failure, the incident or source it came from, or the fact
  that this is a scenario.
- It keeps only the monitoring the recreated system would really have had.
- `npx projen test` runs **`test:clean-purity`** over it: a generic list of tells (scenario words,
  failure-injection tooling, provenance, editorializing like "metastable") plus this scenario's own
  terms. Put the source system's names, class names, incident ids and commit SHAs in the
  `forbiddenTerms` option in `.projenrc.ts` as soon as you know them.
- Import constructs from `@cre/constructs` in either tree. Failure injection, load generation and
  the edge are imported only in `src/deploy` (the purity check fails on them in `src/clean`).

## `src/deploy` — the overlay

Everything that injects, triggers, drives or observes the failure lives here:

- `src/deploy/cdk/app.ts` is the CDK entrypoint. It composes the clean stacks with the overlay.
  The scaffold has no stacks in either tree (`src/clean/cdk` starts empty); add the system's stacks
  in `src/clean/cdk` and create them, then the overlay's, in this file.
- **Dashboards are required, and they live here**: the overlay that injects the failure also makes
  it observable. (This is steering, not a build check.)

## Ground truth (required)

The answer key is `ground-truth/<project-name>.jsonl` (the `groundTruthFile` option), outside
`src/clean`. `npx projen test` runs **`test:ground-truth`** on it once it exists. JSON Lines, one
object per scenario: `id`, `category` (`ngrh_real`), `notes`, `findings[]`.

Every finding has `finding_id` (unique), `type`, `severity`, `location_hint` and `description`.

- **Gaps** (`type: gap`): the actual failure, one or more. `severity` `high` | `medium` | `low`;
  `raf_category` one of `single_point_of_failure`, `excessive_load`, `excessive_latency`,
  `misconfiguration_and_bugs`, `shared_fate`; `rel_category` `REL-01` … `REL-13`;
  `expected_recommendation`.
- **Red herrings** (`type: non-issue`): things a reviewer might plausibly flag but should not.
  `severity: non-issue`, and a `rationale` explaining why it is not a finding.
- `location_hint` is `<path under src/clean> (symbol or note)`, several joined with ` + `; a bare
  file name after ` + ` means the same directory as the previous path. Every path must exist.

The key needs at least one gap and at least one red herring.
