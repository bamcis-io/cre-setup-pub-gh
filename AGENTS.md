# AGENTS.md — cre-setup-pub-gh

Conventions and gotchas for agents working in this generated CRE demo. This demo was
scaffolded from `cre-demo-template`; build the actual demo on top of the green base via PDD.

The `/pdd` planning skill is linked **into this repo** at `.kiro/skills/pdd/` — a symlink to your
`cre-demo-template` clone, so it is never a stale copy (mirrored to
`.claude/skills`), so run `/pdd` here rather than in the template. It asks for the path to your
`cre-demo-template` clone — normally a sibling directory, which it auto-detects and asks you to
confirm. It reads four things live from there, and its Phase 1 blocks if any are missing:
`constructs/README.md`, `patterns.md`, `patterns/`, and `recipe/recipe.md`.

## Project Generation: projen

This repo is **projen-managed** and built on a CRE project type (`CreResilienceDemo`,
`CreFailureScenario` or `CreCustomerSandbox`) vendored at `projenrc/cre/`. Do **not** hand-edit generated
files (`package.json`, `tsconfig*.json`, `.gitignore`, `.projen/*`, the CI files, and
`.kiro/steering/cre-*.md`). The source of truth is `.projenrc.ts`; after editing it, run
`npx projen`. Never edit `projenrc/cre/` here either: change it in `cre-demo-template` and run
`npx projen update-template`.

The always-on steering in `.kiro/steering/cre-*.md` covers what kind of project this is, the
source layout, the construct library, stacks and CI/CD, fidelity, tooling, and git.

## Stacks and the pipeline

The CDK app (`src/**/cdk/app.ts`) is the whole definition of what deploys. Every top-level stack
is a `CreStack` named `app.stackId('<name>')` with the `region` it deploys to; every value it takes
from another stack or a CI/CD variable is a `CreCfnParameter` (`from` / `fromVariable`). After any
change to stacks or their parameters, run `npx projen`: it synthesizes the app and regenerates
`cre-stacks.json` and one deploy job per stack. Commit those with the code; CI never regenerates
them. `npx projen deploy` runs the same deploy locally. Full rules: `.kiro/steering/cre-pipelines.md`.

## Git workflow

Use the projen git tasks; never push, open MRs/PRs, or arm auto-merge by hand.

- New work: `npx projen branch <type>/<description>`
- "Push it": `npx projen push --message "<type>(<scope>): <description>"` (add
  `--branch <type>/<description>` when it asks for one)
- Commit only: `npx projen commit --message "…"`; state check: `npx projen git:status`

Never commit or push without explicit approval. When a task stops, act on its exit code (0 done,
10 refused/nothing changed, 20 build or hook failed, 30 conflict/restored, 40 pushed but MR/PR
incomplete: re-run); don't repair git state by hand. Details: the `cre-git` skill (`.kiro/skills/cre-git/SKILL.md`).

**Editing a skill leaves this repo.** `.kiro/skills/*` (`pdd`, `cre-git`, `locust-cfs`) are symlinks into
your `cre-demo-template` clone; an edit there changes every demo on the machine and does **not**
show in this repo's `git status`. Commit it in the template, on a branch, via an MR.
`npx projen skills:doctor` shows where each skill resolves.

## Bug classes CRE demos keep re-hitting

Carried forward from demos already built on this template. These are **not** hypothetical — each
one shipped, was diagnosed the slow way, and cost hours. Read before writing code, and append
this demo's own findings as PDD uncovers them.

1. **projen's dax shell is not bash.** `${VAR}` braces pass through **literally**, and `set -a`
   is unimplemented (`set: invalid option: -a`). A step that needs bash declares it on that step —
   `{ exec, shell: ['bash', '-c'] }` — which hands the command to bash as one argv element, so no
   quoting invariant applies; this is how every stack step in the generated deploy runs. Where a *fragment* must be
   wrapped in `bash -c '…'` instead (the deploy's `${VAR:-default}` env defaults), that wrapper has one
   invariant: **the generated command must contain no single quotes.** An inner `bash -c '…'`
   broke a real deploy with `unexpected EOF` seven phases in. Audit `.projen/tasks.json` after
   every regen and validate the **generated** `exec` string (`bash -n`), not the snippet you
   wrote — the project type's `deploy-tasks.test.ts` (in `cre-demo-template/packages/cre-projen`)
   does both checks for the deploy tree.
2. **Default botocore timeouts defeat design guarantees.** Hit *six* separate times in one demo.
   Every boto3 client needs an explicit
   `botocore.config.Config(connect_timeout=…, read_timeout=…, retries=…)` at **module scope**,
   sized under the caller's budget. A *hanging* call outlives the caller's own timeout, so the
   `except`/retry path never runs and the failure looks like a hang, not an error.
3. **Spec values that do not exist in reality.** Engine versions, prop names, and API fields that
   appear in a design doc but not in the SDK. Verify against the installed `aws-cdk-lib` `.d.ts`
   or the live API before trusting any design doc — including a design doc you wrote.
4. **Template-vs-deploy-contract mismatches that synth cannot catch.** A `CfnParameter` declared
   but never threaded through the deploy scripts breaks the *deploy*, not the build. Write a test
   that derives the required parameter set **from the synthesized templates** so it fails at build
   time instead of at phase 9 of a live deploy.
5. **IAM grants that are necessary but not sufficient.** `secretsmanager:GetSecretValue` without
   `kms:Decrypt` on a CMK-encrypted secret fails as *"Access to KMS is not allowed."* A knob you
   both read and flip needs **both** grants. Worse when the caller degrades instead of raising —
   one demo answered from a stub for days and looked healthy.
6. **UI fields nothing supplies.** A SPA reading `foo.bar` when the backend returns a bare list
   renders `—` or `?` rather than failing. If the UI reads a field, something must produce it —
   and a test fixture claiming to mirror a real payload **must actually mirror it**, or the test
   is green against a fiction.
7. **An HTTP front door is NOT a secure browser context.** `crypto.randomUUID()` and friends throw
   there; called at module scope they kill the whole SPA, which looks exactly like a backend
   outage. A UI served over plain **HTTP** (no certificate) is not a secure context — never add a
   secure-context-only browser API to it without adding TLS first. CloudFront Signer, the default
   front door, serves HTTPS.
8. **Never pattern-match LLM prose to drive state.** In agentic demos the model paraphrases tool
   output, so prose-matching silently stops firing. Emit a structured event from the tool instead.
9. **A projen spawn step's `env` travels exactly one hop, and a task tree only your mode runs
   has never been run.** `deploy → deploy:upload → deploy:s3` is two hops, so a per-region
   `{ spawn: 'deploy:upload', env: { AWS_REGION } }` renders a correct `tasks.json` and still
   publishes every region to the ambient CI bucket; the failure surfaces one phase later as a
   CloudFormation *"S3 error: The specified bucket does not exist"*. Three demos paid for this.
   Put per-region values in the leaf step as synth-time literals (what `deploy:s3` does now) or
   in a *task*-level `env`, never in a spawn step's `env` — and give any opt-in task tree an
   **execution** test, because a declaration test was green while the deploy was broken.

### Operational facts worth knowing early

- **Endpoints go behind CloudFront Signer (Midway) by default** (`auth/cloudfront-signer`).
  If a demo uses `AllowedCidrSecurityGroup` instead, every deploy resets `AllowedCidr` to
  `0.0.0.0/32` (a deny-all default); re-open it after each deploy and close it when done.
- **`yarn` (berry), never `npm install`** — an `npm install` corrupts `yarn.lock`.
- **Python 3.11+** for anything agentic (`strands-agents` needs ≥3.10; system `python3` may be
  3.9).

### Process that works

Prove a new test **fails without its fix** before landing it — several tests have been green for
the wrong reason until checked that way. And verify claims against the **live account** rather
than inferring them from a green pipeline: the worst defects found so far were only findable that
way.

## Detailed Documentation

See `CLAUDE.md` for the Claude Code mirror of these rules. Add project-specific conventions
below as the demo is built out via `/pdd` (vendored at `.kiro/skills/pdd/`; run it from this repo).
