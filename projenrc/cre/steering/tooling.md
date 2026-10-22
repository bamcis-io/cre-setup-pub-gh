---
inclusion: always
---
# CRE tooling in this repo

Run these as projen tasks from the repo root. `npx projen --help` lists every task.

## The loop

1. `npx projen branch <type>/<description>`: start the work.
2. Change the code. A new or rewired stack goes in the CDK app (`CreStack` + `region`,
   `CreCfnParameter` for every value it takes: `cre-pipelines.md`).
3. `npx projen`: regenerates the pipeline and `cre-stacks.json` from the app. Run it after any
   change to stacks, their parameters, or `.projenrc.ts`.
4. `npx projen build`: must pass.
5. `npx projen push --message "<type>(<scope>): <description>"`, with the user's approval. The MR
   merges when CI passes, and the pipeline deploys every stack.

| Task | Use it to |
|---|---|
| `npx projen branch` / `push` / `commit` / `git:status` | All git work. Never push, open MRs/PRs or arm auto-merge by hand. Full rules and exit codes: the `cre-git` skill |
| `npx projen watch` | Follow one MR/PR from its checks to the deploy of its merge commit (below). Read-only |
| `npx projen` | Regenerate everything: synthesizes the CDK app (no bundling, seconds), reads its stacks, writes `cre-stacks.json` and one deploy job per stack |
| `npx projen build` | Regenerate, synth, check, test, lint, package. Must pass before anything is pushed. With `containerImages` and no Docker here it skips building the images (warned; CI builds them) |
| `npx projen check:generated` | CI runs it first: fails when the committed generated files aren't what `npx projen` makes (fix: `npx projen`, commit the result). Skipped locally |
| `npx projen check:deploy-prereqs` | Part of every build: fails when a stack deploys to a region with no assets bucket, needs a CI/CD variable setup never set, or takes a secret (a `noEcho` parameter) from an unmasked variable. It prints the `setup:aws` / `setup:repo` command that closes the gap (they record it in `cre-setup.json`; commit that) |
| `npx projen deploy` | The pipeline's deploy, on your machine: builds first (`--no-build` skips), then `deploy:upload`, `deploy:stack` per stack in order with its CI job's variables, `deploy:prune`. `--stack <id>` (repeatable) deploys only those, taking the outputs of the stacks they need from AWS. Stops first when your credentials are for another account than `cre-setup.json`'s, or can't assume a stack's role (a cross-account DNS stack trusts only the CI runner role: let CI deploy it). Variables come from the environment, `.cre/deploy.env` (written by setup, gitignored) or a prompt. `--profile <p>` |
| `npx projen stacks` | Each planned stack's deployed status, plus **orphans**: this project's stacks no longer in the app (renamed or removed), which keep running until deleted |
| `npx projen stacks:outputs` | The deployed stacks' outputs (`EdgeUrl`, `EdgeLocustUrl`, ids) as `<stack> <Key>=<Value>`; `--stack <id>` for one |
| `npx projen teardown` | Delete the stacks in reverse deploy order. Prints what it would delete and exits 12; `--yes` deletes, after the user agrees. `--orphans` only the orphans, `--stack <id>` one. Buckets, setup roles and retained resources stay; a stack in another account is skipped (named) |
| `npx projen ci:rerun` | Retry the failed jobs of main's latest deploy (`--sha <sha>` another commit's); `--all` runs the whole deploy again (a redeploy without a commit). Then `npx projen watch --sha <sha>` |
| `npx projen cfs:onboard` | Onboard the edge distribution at CloudFrontSigner, once, after the first deploy of the `CreEdgeStack` (always `us-east-1`; it reads `EdgeDomainName` from the plan's edge stack; Midway only). Tell the user it's needed after that first edge deploy: until then the edge answers 403. `--status` checks, `--offboard` undoes it |
| `npx projen cfs:curl GET <url>` | Call anything behind CloudFront Signer (a Locust UI, an operator page) with your Midway cookie, no browser. `CFS_URL=<base>` lets you pass a `/path` |
| `npx projen setup:aws` / `setup:repo` / `setup:dns` / `setup:github-app` | Re-run or repair the account, repo, DNS or (GitHub) automation App setup (`python3 projenrc/cre/scripts/setup/*.py`; idempotent; defaults from `cre-setup.json`). `setup:aws` also makes buckets for every region in `cre-stacks.json`. `setup:repo prepare --var KEY=VALUE[:protected][:masked]` (GitLab; `:masked` for a secret) / `--secret KEY=VALUE` (GitHub) |
| `npx projen setup:github-public` | GitHub, a private repo going public, only when the user asks. From a clean `main`: it refuses a history holding a credential, prints what becomes readable (exit 12); with `--yes`, after the user agrees, it makes the repo public, protects `main`, regenerates the workflows (GitHub auto-merge instead of the build workflow's merge job), pushes them as a PR and waits for it to merge. Re-running carries on where it stopped |
| `npx projen update-template` | Pull your `cre-demo-template` clone (the one `.cre/template.json` records, else the sibling), then refresh `projenrc/cre/` (the project type, its scripts and this steering), `cre-constructs/`, the skill links and (unless edited here) `AGENTS.md` / `CLAUDE.md` from it, show the template's CHANGELOG since this project's version, and rebuild. A clone on another branch is refused (`--allow-branch` to test one). `--from <path>` if the clone moved |
| `npx projen skills:doctor` | Check the `.kiro/skills` symlinks into the template resolve |

## Watching an MR/PR to its deploy

Only when it's needed: the user asks whether a change deployed, or something is being
troubleshot. Not after every push: the pipeline merges and deploys on its own.

| Command | Follows |
|---|---|
| `npx projen watch` | the MR/PR of the current branch |
| `npx projen watch <n>` | MR !n / PR #n |
| `npx projen watch --branch cre/upgrade-main` (GitLab) / `--branch github-actions/upgrade-main` (GitHub) | the latest weekly dependency-upgrade MR/PR |
| `npx projen watch --sha <sha>` | only the deploy of that commit on main (the first push) |

It reads every ID from GitLab or GitHub and keys each stage on the one before: the checks of the
MR/PR's current head commit, then the merge and the commit it put on `main`, then the pipeline (or
`deploy` workflow run) of exactly that commit. Never look pipelines up yourself by "the latest
one": another merge, a scheduled run or an MR pipeline is easily mistaken for it. `--until checks`
or `--until merged` stops earlier. It is read-only: it never retries, merges or starts anything.

Exit codes: **0** reached the stage (the last stdout line is `RESULT code=0 stage=deployed ...`);
**30** a job failed: it prints the job, its URL and the end of its log, so report or fix that;
**20** the MR/PR was closed unmerged; **40** timed out or a pipeline never started (the `stage` says
where); **10** nothing to watch; **11** credentials. The run takes as long as the pipeline: run it
with a long timeout (default 3600 s, `--timeout` to change).

## Defaults the design starts from

- **Constructs: `@cre/constructs` first** (`cre-constructs.md`). It is a dependency; never edit
  `cre-constructs/`.
- **Auth: CloudFront Signer.** Any endpoint a person reaches (UI, operator page, Locust, API) goes
  behind the edge (a `CreEdgeStack` with the origin as its route). Another mechanism only when that doesn't fit
  (non-HTTP, a machine caller), recorded as a deviation in the design.
- **Certificates: one `*.<DOMAIN>` per region** (`ValidatedCertificate`), when `cre-setup.json` has
  a `domain`.
- **Stacks, wiring and CI: `cre-pipelines.md`.** Every top-level stack a `CreStack` with its
  `region`; every value from another stack or a CI/CD variable a `CreCfnParameter` (never
  exports); the pipeline is generated from the app and only calls projen tasks.
- **Dashboard: a `$PROJECT_NAME-dashboard` stack** (`CreDashboardStack`, `app.stackId('dashboard')`),
  with widgets designed for this project. The scaffold has no stacks; add it (required for a
  resilience demo).

## How deploys authenticate

Every deploy job runs as `AWS_CREDS_TARGET_ROLE` (the CI runner role) and hands CloudFormation a
service role (`CFN_EXECUTION_ROLE_ARN`, or the stack's `executionRole`) as `--role-arn`; the runner
role itself only drives CloudFormation, the assets buckets and ECR. A step that calls AWS directly
outside CloudFormation needs a permission added to the runner role's policies (setup never changes
an existing runner role's permissions). A stack in another account (a `CreDnsStack` with
`assumeRole: '$DNS_ROLE_ARN?'`) assumes that role from the runner role first.

## Skills are shared tooling

`.kiro/skills/*` (`pdd`, `cre-git`, `locust-cfs`) are **symlinks** into your `cre-demo-template` clone.
Editing one changes the template, and so every demo on the machine, and does not show in this
repo's `git status`. Commit such an edit in the template, on a branch, via an MR there.
