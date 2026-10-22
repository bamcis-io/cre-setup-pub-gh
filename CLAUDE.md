# CLAUDE.md

Guidance for Claude Code when working in this generated CRE demo. This demo was scaffolded
from `cre-demo-template`; the actual demo is built on the green base via PDD.

The `/pdd` planning skill is linked **into this repo** at `.kiro/skills/pdd/` — a symlink into your
`cre-demo-template` clone, so it is never a stale copy — reachable through
the `.claude/skills` symlink, so run `/pdd` here rather than in the template. It asks for the path
to your `cre-demo-template` clone — normally a sibling directory, which it auto-detects and asks
you to confirm. It reads four things live from there, and its Phase 1 blocks if any are missing:
`constructs/README.md`, `patterns.md`, `patterns/`, and `recipe/recipe.md`.

> **Read [`AGENTS.md`](AGENTS.md) first.** It carries the bug classes CRE demos keep
> re-hitting (dax-shell quoting, default botocore timeouts, spec values that don't exist,
> deploy contracts synth can't check, UI fields nothing supplies) and the operational facts
> that cost hours to rediscover. Those are deliberately NOT duplicated here — two copies
> drift, and the drifted one gets believed.

## Steering: read these first

Kiro loads `.kiro/steering/*.md` automatically; Claude Code does not. Read them at the start of a
session. They are generated from the CRE project type, so they are current:

- `.kiro/steering/cre-<type>.md`: what kind of project this is (resilience demo, failure scenario,
  or customer sandbox) and what that requires
- `.kiro/steering/cre-tooling.md`: the commands and the change loop. Start here
- `.kiro/steering/cre-pipelines.md`: how stacks are defined (`CreStack`, `CreCfnParameter`) and
  how the generated pipeline deploys them
- `.kiro/steering/cre-constructs.md`: the construct library and when to use each
- `.kiro/steering/cre-project-structure.md`, `cre-fidelity.md`
- any demo-specific steering next to them

Git rules are the `cre-git` skill (`.kiro/skills/cre-git/SKILL.md`)

## Project Generation: projen

This repo is **projen-managed** and built on a CRE project type vendored at `projenrc/cre/`. Do
**not** hand-edit generated files; they are overwritten by `npx projen`. The source of truth is
`.projenrc.ts`. Never edit `projenrc/cre/`: change it in `cre-demo-template`, then
`npx projen update-template`.

## Git workflow

Use the projen git tasks; never push, open MRs/PRs, or arm auto-merge by hand.

- New work: `npx projen branch <type>/<description>`
- "Push it": `npx projen push --message "<type>(<scope>): <description>"` (add
  `--branch <type>/<description>` when it asks for one)
- Commit only: `npx projen commit --message "…"`; state check: `npx projen git:status`

Never commit or push without explicit approval. Act on the exit code (0 done, 10 refused/nothing
changed, 20 build or hook failed, 30 conflict/restored, 40 pushed but MR/PR incomplete: re-run);
don't repair git state by hand. Details: the `cre-git` skill (`.kiro/skills/cre-git/SKILL.md`).

**Editing a skill leaves this repo.** `.kiro/skills/*` (`pdd`, `cre-git`, `locust-cfs`) are symlinks into
your `cre-demo-template` clone; an edit changes every demo on the machine and doesn't show in this
repo's `git status`. Commit it in the template via an MR there.
