import { gitlab } from 'projen';
import type { awscdk } from 'projen';
import { GITLAB, PRUNE_JOB, render, UPLOAD_JOB } from '../components/stack-jobs';
import type { JobDefaults, StackJob } from '../components/stack-jobs';
import { CRE_SCRIPTS } from '../paths';
import { craneInstall } from './container-tools';

/**
 * Base image for every job. `node:20-alpine` is small, multi-arch, ships
 * npm/yarn via corepack, and Alpine's apk repo carries the rest of the toolchain
 * (aws-cli, git, python, bash). Every job uses it except build:docker.
 *
 * Container builds (kaniko executor image, then a crane push in the deploy) are emitted ONLY
 * when containerImages is true. The shared GitLab runner fleet blocks Docker-in-Docker
 * (seccomp) and rootless podman/buildah (CLONE_NEWUSER), hence kaniko.
 */
const BASE_IMAGE = 'node:20-alpine';

/**
 * Pre-job toolchain install for every job. `corepack enable` works because
 * node:20-alpine bundles corepack.
 *   - aws-cli + git + python3 + bash + tar/gzip/zip/unzip from Alpine community
 *   - python3 is needed by scripts/build/package.py (asset staging) and
 *     scripts/build/deploy-stack.sh (params + outputs JSON handling)
 */
const TOOLCHAIN_INSTALL = [
  'apk add --no-cache bash aws-cli git python3 py3-pip tar gzip zip unzip curl',
];

const YARN_SETUP = [
  'corepack enable',
  'corepack prepare yarn@stable --activate',
  'yarn install --immutable',
];

/**
 * Kaniko executor image (the container build). All other container build
 * tools fail on the shared GitLab runner fleet: Docker-in-Docker is blocked by seccomp,
 * and rootless podman/buildah trip `unshare(CLONE_NEWUSER): Operation not permitted`.
 * Kaniko builds OCI images in a chroot — no daemon, no user namespaces. The `:debug`
 * tag is distroless + busybox (no apk/node/yarn), so the build:docker job's beforeScript
 * MUST be empty and its script invoked via `sh`.
 */
const KANIKO_VERSION = 'v1.23.2';
const KANIKO_IMAGE = `gcr.io/kaniko-project/executor:${KANIKO_VERSION}-debug`;


/**
 * Extra install step for deploy:upload: downloads crane (static Go binary) so
 * deploy-docker.sh can push image tarballs to ECR without a docker daemon. Only added
 * to deploy:upload's beforeScript when containerImages is true.
 */
const CRANE_INSTALL = [craneInstall('/usr/local/bin')];

/** The weekly dependency upgrade job, and the branch its MR comes from. */
export const UPGRADE_JOB = 'upgrade:main';
export const UPGRADE_BRANCH = 'cre/upgrade-main';
export const UPGRADE_TITLE = 'chore(deps): upgrade dependencies';

/**
 * The weekly upgrade, GitLab's counterpart of projen's GitHub `upgrade-main` workflow. It runs only
 * in a pipeline started by the project's schedule (`repo_gitlab.py finalize` creates it, weekly, on
 * main, with CRE_UPGRADE=1): `npx projen upgrade`, and when anything changed, a commit pushed to
 * UPGRADE_BRANCH with push options that open the MR, set it to merge when its pipeline succeeds,
 * squash and delete the branch. The MR pipeline builds it; the merge deploys, like any MR.
 *
 * The push uses CRE_UPGRADE_TOKEN, a project access token (write_repository only, this project
 * only) that setup stores as a masked, Protected variable: a job's own CI_JOB_TOKEN can't push
 * (gitlab.aws.dev answers 403 even with `ci_push_repository_for_job_token_allowed`).
 */
function upgradeJob(): gitlab.Job {
  const push = [
    'git push --force',
    '"https://cre-upgrade:${CRE_UPGRADE_TOKEN}@${CI_SERVER_HOST}/${CI_PROJECT_PATH}.git"',
    `HEAD:refs/heads/${UPGRADE_BRANCH}`,
    '-o merge_request.create',
    '-o "merge_request.target=${CI_DEFAULT_BRANCH}"',
    `-o "merge_request.title=${UPGRADE_TITLE}"`,
    '-o merge_request.merge_when_pipeline_succeeds',
    '-o merge_request.remove_source_branch',
    '-o merge_request.squash',
  ].join(' ');
  return {
    stage: 'upgrade',
    rules: [{ if: '$CI_PIPELINE_SOURCE == "schedule" && $CRE_UPGRADE == "1"' }],
    needs: [],
    interruptible: true,
    script: [
      'test -n "${CRE_UPGRADE_TOKEN:-}" || { echo "CRE_UPGRADE_TOKEN is not set: run npx projen setup:repo finalize" >&2; exit 1; }',
      'npx projen upgrade',
      'if [ -z "$(git status --porcelain)" ]; then echo "Dependencies are up to date: nothing to do."; exit 0; fi',
      'git config user.name "cre-upgrade" && git config user.email "cre-upgrade@noreply.${CI_SERVER_HOST}"',
      `git checkout -B ${UPGRADE_BRANCH}`,
      'git add -A',
      `git commit -q -m "${UPGRADE_TITLE}" -m "Upgrades project dependencies (scheduled pipeline \${CI_PIPELINE_URL})."`,
      push,
    ],
  };
}

/**
 * Wires the GitLab pipeline onto the project. Every job runs a projen task (the one exception,
 * build:docker on the node-less kaniko image, runs the script behind the build:docker task).
 *
 * Stages:
 *   build  — runs on MRs + main:
 *     build:cdk      `yarn ci:build:cdk` — cdk synth, the stack-wiring and stack-plan checks, lint,
 *                    unit tests, CFN asset staging (node image). Artifacts: assets/, cdk.out/.
 *     build:container-plan, build:docker  (containerImages only) — list the images, build each
 *                    with kaniko into assets/containers/.
 *     build:package  `yarn ci:build:package` — zip assets/ → dist/content.zip.
 *
 *   deploy — runs on the default branch only (split-trust analog: gated to main).
 *            The gate is only as strong as the variable: AWS_CREDS_TARGET_ROLE MUST be a
 *            **Protected** CI/CD variable and `main` a protected branch. The credential
 *            vendor runs as a pre_clone script for EVERY job that can see the variable, so
 *            an unprotected one hands the deploy role to every MR pipeline — i.e. to MR
 *            code (yarn install, tests, or an MR that edits .gitlab-ci.yml).
 *     deploy:upload  `npx projen deploy:upload` — publish templates, assets (and images) to every
 *                    listed region's bucket.
 *     deploy:<stack> one job per stack, named after it (`deploy:<project>-edge`), `npx projen
 *                    deploy:stack`, with the variables stack-jobs.ts generates for it. `needs`:
 *                    deploy:upload plus the jobs of the stacks it takes outputs from (and its
 *                    `dependsOn`); their dist/<stack>.env dotenv artifacts carry the outputs.
 *     deploy:prune   `npx projen deploy:prune`, after every stack job.
 *
 * AWS credentials are minted by the GitLab runner wrapper from the project-level
 * `AWS_CREDS_TARGET_ROLE` CI/CD variable (kept out of the repo), the same role in every job; a
 * stack in another account assumes its `assumeRole` from it (role chaining in deploy-stack.sh).
 * `AWS_DEFAULT_REGION` is the stack's region in each stack job.
 *
 * Operators set these CI/CD variables in GitLab project settings:
 *   AWS_CREDS_TARGET_ROLE   IAM role ARN the runner wrapper assumes for AWS calls.
 *                           Protected: yes (see the deploy stage note above).
 *   ASSETS_BUCKET_PREFIX    Common prefix; the regional bucket is `<prefix>-<region>`, one per
 *                           region a stack deploys to (setup:aws creates them).
 *   Every variable the stacks name (CreCfnParameter fromVariable, assumeRole, executionRole;
 *   CFN_EXECUTION_ROLE_ARN by default). GitLab passes project CI/CD variables to every job, so
 *   each stack job references the ones it uses, and fails fast on a required one that's empty.
 *   cre-stacks.json's `environment` lists them.
 *
 * AWS_REGION is NOT in that list: it comes from `opts.awsRegion`, which the recipe
 * substitutes from the target region the builder gave at STEP 1. It is deliberately
 * REQUIRED (no default) — an `?? 'us-east-1'` fallback is what previously caused a
 * demo requested in us-east-2 to deploy to us-east-1 silently, and a compile error
 * beats a wrong region. A CI/CD variable of the same name still overrides it.
 */
export function createGitlabWorkflow(
  project: awscdk.AwsCdkTypeScriptApp,
  opts: { awsRegion: string; containerImages?: boolean; jobs: readonly StackJob[]; defaults: JobDefaults; pruneAssets?: boolean },
): gitlab.GitlabConfiguration {
  // A scheduled pipeline is the weekly upgrade (UPGRADE_JOB): it neither builds nor deploys main.
  const buildRules = [
    { if: '$CI_PIPELINE_SOURCE == "merge_request_event"' },
    { if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_PIPELINE_SOURCE != "schedule"' },
  ];
  const artifacts = (paths: string[]): gitlab.Artifacts => ({ paths, expireIn: '1 day' });

  // Everything every job shares lives in `default`: image, tags, toolchain + yarn install, the
  // yarn cache and the retry policy. A job states only what is its own (stage, needs, rules,
  // script, artifacts, variables). build:docker is the one job that overrides the base, because
  // the kaniko image has no apk/node/yarn.
  const ci = new gitlab.GitlabConfiguration(project, {
    default: {
      image: { name: BASE_IMAGE },
      // arm64 shared runner fleet.
      tags: ['arch:arm64', 'size:medium'],
      beforeScript: [...TOOLCHAIN_INSTALL, ...YARN_SETUP],
      cache: [
        {
          // Branch-scoped cache, falling back to the default branch's cache so
          // first-MR builds warm-start instead of downloading every dep.
          key: '${CI_COMMIT_REF_SLUG}-yarn',
          fallbackKeys: ['${CI_DEFAULT_BRANCH}-yarn'],
          paths: ['.yarn/cache/', 'node_modules/'],
        },
      ],
      // Transient infrastructure and throttling only; a real failure is never retried.
      retry: {
        max: 1,
        when: ['api_failure', 'runner_system_failure', 'stuck_or_timeout_failure'],
      },
    },
    stages: ['build', 'deploy', 'upgrade'],
    variables: {
      // PROJECT_NAME is plumbed here so GitLab variable expansion (ASSETS_PREFIX) resolves. The
      // same value is exposed to projen tasks via .projenrc.ts (tasks.addEnvironment).
      PROJECT_NAME: project.name,
      // Unique prefix per pipeline, scoped under $PROJECT_NAME/. Every deploy job of the
      // pipeline (and a retried one) uses the same prefix.
      ASSETS_PREFIX: '${PROJECT_NAME}/${CI_PIPELINE_ID}/',
      // The region the builder asked for at recipe STEP 1, substituted into .projenrc.ts: the
      // region deploy:upload and deploy:prune run in. Each stack job sets its own.
      AWS_REGION: opts.awsRegion,
      // Defaults for deploy variables (GitLab has no `${X:-default}`). A project CI/CD
      // variable of the same name overrides them.
      ...opts.defaults,
      // ASSETS_BUCKET_PREFIX has no sensible default — always set it as a CI/CD
      // variable (the operator creates one bucket per region).
    },
  });

  // ---- build jobs (container jobs gated on containerImages) ----------------
  //
  // Without containerImages: build:cdk + build:package. With it:
  //   build:cdk → build:container-plan → build:docker → build:package.
  const buildJobs: Record<string, gitlab.Job> = {
    // CDK synthesis, lint, unit tests, CFN asset staging. Invokes the provider-agnostic
    // ci:build:cdk task.
    'build:cdk': {
      stage: 'build',
      rules: buildRules,
      script: ['yarn ci:build:cdk'],
      artifacts: artifacts(['assets/', 'cdk.out/']),
    },
  };

  if (opts.containerImages) {
    // Container build plan. Reads cdk.out/*.assets.json on the node image (python3
    // present) → assets/containers/manifest.json + dist/container-plan.tsv (the
    // python-free plan build:docker reads on the distroless kaniko image).
    buildJobs['build:container-plan'] = {
      stage: 'build',
      needs: ['build:cdk'],
      rules: buildRules,
      script: ['yarn ci:build:container-plan'],
      artifacts: artifacts(['assets/containers/manifest.json', 'dist/container-plan.tsv']),
    };

    // Container image build. Runs on the kaniko image directly (distroless + busybox:
    // no apk/node/yarn), so beforeScript is EMPTY and the script is invoked via `sh`.
    // Reads dist/container-plan.tsv and produces assets/containers/<hash>.tar.gz.
    buildJobs['build:docker'] = {
      stage: 'build',
      image: { name: KANIKO_IMAGE, entrypoint: [''] },
      needs: ['build:cdk', 'build:container-plan'],
      rules: buildRules,
      variables: { KANIKO_IMAGE },
      beforeScript: [],
      // Content-addressed image cache (dist/.cache/containers/<hash>.tar.gz). Keyed per branch
      // so unchanged images are reused across pipelines instead of re-pulling base images from
      // public.ecr.aws (rate limits) and recompiling from scratch. A changed image → new hash →
      // cache miss → one rebuild, then re-cached. Restored/saved by the runner's cache helper,
      // so it works even on the distroless kaniko image; build-docker.sh skips kaniko on a hit.
      cache: [
        {
          key: 'container-tarballs-$CI_COMMIT_REF_SLUG',
          paths: ['dist/.cache/containers/'],
        },
      ],
      script: [`sh ${CRE_SCRIPTS}/build/build-docker.sh`],
      artifacts: artifacts(['assets/containers/']),
    };
  }

  // Zip the staged assets/ tree into dist/content.zip — the single artifact the deploy
  // stage consumes. needs[] is conditional so the no-load-gen package job does NOT wait
  // on (non-existent) container jobs.
  buildJobs['build:package'] = {
    stage: 'build',
    needs: opts.containerImages
      ? ['build:cdk', 'build:container-plan', 'build:docker']
      : ['build:cdk'],
    rules: buildRules,
    script: ['yarn ci:build:package'],
    artifacts: artifacts(['dist/content.zip']),
  };

  // ---- deploy jobs -----------------------------------------------------------
  //
  // Every deploy job: default branch only (GitLab's split-trust analog), the `demo` environment
  // (deploys show under Operate > Environments; make it protected to control who deploys), never
  // interrupted mid-CloudFormation (an interrupted change-set leaves the stack *_IN_PROGRESS and
  // fails the next deploy), and serialised per job across pipelines by resource_group, so two
  // pipelines never update the same stack at once while independent stacks still run in parallel.
  const deployJob = (name: string, job: gitlab.Job): gitlab.Job => ({
    stage: 'deploy',
    rules: [{ if: '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_PIPELINE_SOURCE != "schedule"' }],
    environment: { name: 'demo' },
    interruptible: false,
    // Above deploy-stack.sh's own wait (STACK_WAIT_SECONDS, 100 min), so a slow stack fails there,
    // with its events, instead of the job being killed with the stack mid-update.
    timeout: '2h',
    // GitLab refuses a resource_group key with `:` (deploy:<stack>) and silently drops it.
    resourceGroup: name.replace(/:/g, '-'),
    ...job,
  });

  const deployJobs: Record<string, gitlab.Job> = {
    [UPLOAD_JOB.gitlab]: deployJob(UPLOAD_JOB.gitlab, {
      needs: ['build:package'],
      // crane only with container images (it pushes the pre-built tarballs).
      ...(opts.containerImages ? { beforeScript: [...TOOLCHAIN_INSTALL, ...CRANE_INSTALL, ...YARN_SETUP] } : {}),
      variables: { AWS_DEFAULT_REGION: '$AWS_REGION' },
      script: ['npx projen deploy:upload'],
    }),
  };
  for (const j of opts.jobs) {
    deployJobs[j.gitlabJob] = deployJob(j.gitlabJob, {
      // Artifacts of a needed job are downloaded by default: deploy:upload has none the stack
      // needs, and each stack job's dist/<stack>.env dotenv brings the outputs it takes.
      needs: [UPLOAD_JOB.gitlab, ...j.needs.map((n) => opts.jobs.find((x) => x.stack === n)!.gitlabJob)],
      variables: Object.fromEntries(Object.entries(j.env).map(([k, v]) => [k, render(v, GITLAB)])),
      script: ['npx projen deploy:stack'],
      artifacts: {
        paths: [`dist/${j.stack.id}.env`],
        reports: { dotenv: [`dist/${j.stack.id}.env`] },
        expireIn: '1 day',
      },
    });
  }
  if (opts.pruneAssets !== false) {
    deployJobs[PRUNE_JOB.gitlab] = deployJob(PRUNE_JOB.gitlab, {
      // After every stack job, and after the upload: with no stacks, `needs: []` would mean "start
      // at once", in parallel with the upload.
      needs: [{ job: UPLOAD_JOB.gitlab, artifacts: false }, ...opts.jobs.map((j) => ({ job: j.gitlabJob, artifacts: false }))],
      variables: { AWS_DEFAULT_REGION: '$AWS_REGION' },
      script: ['npx projen deploy:prune'],
    });
  }

  ci.createNestedTemplates({
    build: { jobs: buildJobs },
    deploy: { jobs: deployJobs },
    upgrade: { jobs: { [UPGRADE_JOB]: upgradeJob() } },
  });

  return ci;
}
