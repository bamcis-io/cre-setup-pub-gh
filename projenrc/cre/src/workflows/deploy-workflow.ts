// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * The GitHub deploy workflow: the per-stack deploy jobs, run after a PR merges to main.
 *
 * The PR flow is projen's (as in multi-az-workshop): `npx projen push` opens the PR with the
 * `auto-approve` and `auto-merge` labels; projen's auto-approve workflow approves a trusted
 * author's labelled PR and its auto-queue workflow enables auto-merge (squash). Branch protection
 * on main requires the `build` check and an approval, so the PR merges only once the build passed.
 * The merge is a push to main, which runs this workflow. That is the same model as GitLab: main
 * takes no direct pushes, and every change deploys when its MR/PR merges.
 *
 * Triggers:
 *  - push to main: the merged PR (merged by projen's auto-merge with the builder's GitHub App token, not GITHUB_TOKEN, so it triggers this);
 *  - workflow_dispatch: redeploy main by hand.
 *
 * Jobs: `build` (checks out the exact commit, `npx projen build`, uploads dist/content.zip),
 * `deploy-upload`, one job per stack (stack-jobs.ts, the same needs and variables as the GitLab
 * pipeline and the local deploy) and `deploy-prune`. Only code that is on main runs with
 * AWS_CREDS_TARGET_ROLE, and the runner role trusts only main's OIDC subject (setup).
 *
 * Concurrency: each deploy job is serialised per repository and never cancelled mid-CloudFormation
 * (GitLab's per-job `resource_group`), so two merges never update one stack at once while the
 * independent stacks of one run still deploy in parallel.
 */

import { GithubWorkflow } from 'projen/lib/github';
import type { GitHub } from 'projen/lib/github';
import { JobPermission } from 'projen/lib/github/workflows-model';
import { craneInstall, GITHUB_QEMU_STEP } from './container-tools';
import { GITHUB, PRUNE_JOB, render, UPLOAD_JOB } from '../components/stack-jobs';
import type { JobValue, StackJob } from '../components/stack-jobs';

/** The build job's artifact carrying dist/content.zip. */
const CONTENT_ARTIFACT = 'demo-content';
/** The build job, which every deploy job needs. */
export const BUILD_JOB = 'build';

/**
 * Options for {@link createDeployWorkflow}.
 *
 * `containerImages` is the GitHub half of the single load-gen toggle: the build job builds the
 * images into dist/content.zip (`build:docker`), and `deploy-upload` pushes the tarballs with crane.
 */
export interface DeployWorkflowOptions {
  /** The project name: `PROJECT_NAME`, the same literal the GitLab pipeline sets. */
  readonly projectName: string;
  /** Region for the upload and prune jobs; each stack job uses its own stack's region. */
  readonly awsRegion: string;
  readonly containerImages?: boolean;
  /** One deploy job per stack (stack-jobs.ts). */
  readonly jobs: readonly StackJob[];
  /** Delete old deploy prefixes after every stack deployed. @default true */
  readonly pruneAssets?: boolean;
}

export function createDeployWorkflow(github: GitHub, opts: DeployWorkflowOptions): void {
  // One run at a time (#7): runs never interleave, so every stack ends at the same commit. A run
  // already deploying finishes; of the runs waiting, GitHub keeps only the newest, which deploys
  // main's latest commit to every stack anyway.
  const workflow = new GithubWorkflow(github, 'deploy', {
    limitConcurrency: true,
    concurrencyOptions: { group: 'deploy-${{ github.repository }}', cancelInProgress: false },
  });
  workflow.on({
    push: { branches: ['main'] },
    workflowDispatch: {},
  });

  // The commit this run deploys: the merge commit on push, main's head on a dispatch from main.
  const checkout = { name: 'Checkout', uses: 'actions/checkout@v4', with: { ref: '${{ github.sha }}' } };
  const setup = [
    checkout,
    { name: 'Setup Node.js', uses: 'actions/setup-node@v4', with: { 'node-version': '20' } },
    { name: 'Enable Corepack', run: 'corepack enable' },
    // The locked dependencies: without node_modules `npx projen` would fetch whatever projen is
    // latest on npm and run tasks.json under a different engine from the one the build used.
    { name: 'Install dependencies', run: 'yarn install --immutable' },
  ];
  // Deploys only from main (a dispatch from another branch does nothing; its OIDC subject isn't
  // trusted anyway).
  const onMain = "github.ref == 'refs/heads/main'";

  workflow.addJob(BUILD_JOB, {
    if: onMain,
    runsOn: ['ubuntu-24.04-arm'],
    permissions: { contents: JobPermission.READ },
    env: { CI: 'true', PROJECT_NAME: opts.projectName },
    steps: [
      ...setup,
      // build:docker makes the images when containerImages is on.
      ...(opts.containerImages ? [GITHUB_QEMU_STEP] : []),
      // The build regenerates the pipeline and plan first, so check the committed ones before (#15).
      { name: 'Generated files are current', run: 'npx projen check:generated' },
      { name: 'Build', run: 'npx projen build' },
      {
        name: 'Upload content artifact',
        uses: 'actions/upload-artifact@v7',
        with: { 'name': CONTENT_ARTIFACT, 'path': 'dist/content.zip', 'retention-days': 7, 'if-no-files-found': 'error' },
      },
    ],
  });
  addDeployJobs(workflow, opts, setup);
}

/** Every deploy-variable reference inside a job value. */
function varRefs(x: JobValue): Extract<JobValue, { kind: 'var' }>[] {
  if (x.kind === 'var') return [x];
  if (x.kind === 'concat') return x.parts.flatMap(varRefs);
  if (x.kind === 'lines') return x.lines.flatMap(([, y]) => varRefs(y));
  return [];
}

// Every deploy job has the same base (permissions, concurrency, the shared env, the setup steps and
// the AWS credentials); a stack job adds its needs and the variables stack-jobs.ts generates for it.
function addDeployJobs(workflow: GithubWorkflow, opts: DeployWorkflowOptions, setup: any[]): void {
  const credentials = (region: string) => ({
    name: 'Configure AWS credentials',
    uses: 'aws-actions/configure-aws-credentials@v5.1.0',
    with: {
      'role-to-assume': '${{ secrets.AWS_CREDS_TARGET_ROLE }}',
      'aws-region': region,
      'mask-aws-account-id': true,
    },
  });
  const job = (id: string, needs: string[], steps: any[]) => {
    workflow.addJob(id, {
      needs,
      // One run of each deploy job at a time per repository, never cancelled mid-CloudFormation
      // (a cancelled change set leaves the stack *_IN_PROGRESS and fails the next one).
      concurrency: { 'group': `${id}-\${{ github.repository }}`, 'cancel-in-progress': false },
      runsOn: ['ubuntu-latest'],
      permissions: { contents: JobPermission.READ, idToken: JobPermission.WRITE, actions: JobPermission.READ },
      env: {
        CI: 'true',
        PROJECT_NAME: opts.projectName,
        // One prefix for every job of the run (and a re-run of one), as GitLab's CI_PIPELINE_ID.
        ASSETS_PREFIX: `${opts.projectName}/\${{ github.run_id }}/`,
        ASSETS_BUCKET_PREFIX: '${{ secrets.ASSETS_BUCKET_PREFIX }}',
      },
      steps,
    });
  };

  // The build job already built any images into dist/content.zip; this job only pushes them.
  const craneSteps = opts.containerImages
    ? [{
      name: 'Install crane (pushes the pre-built images)',
      run: `${craneInstall('$HOME/.local/bin')} && echo "$HOME/.local/bin" >> "$GITHUB_PATH"`,
    }]
    : [];
  job(UPLOAD_JOB.github, [BUILD_JOB], [
    ...setup,
    { name: 'Download content artifact', uses: 'actions/download-artifact@v8', with: { name: CONTENT_ARTIFACT, path: 'dist' } },
    credentials(opts.awsRegion),
    ...craneSteps,
    { name: 'Publish templates, assets and images', run: 'npx projen deploy:upload' },
  ]);

  const outputsArtifact = (j: StackJob) => `stack-outputs-${j.githubJob}`;
  for (const j of opts.jobs) {
    const needs = j.needs.map((n) => opts.jobs.find((x) => x.stack === n)!);
    // Each deploy variable it uses, under its own name (REQUIRED_VARS checks them; the role
    // fallback reads CFN_EXECUTION_ROLE_ARN).
    const refs = Object.values(j.env).flatMap(varRefs);
    const variables = Object.fromEntries(j.variables.map((v) => {
      const own = refs.find((x) => x.name === v);
      return [v, render(own ?? { kind: 'var', name: v, optional: false }, GITHUB)];
    }));
    job(j.githubJob, [UPLOAD_JOB.github, ...needs.map((n) => n.githubJob)], [
      ...setup,
      // The outputs of the stacks it needs, as GitLab's dotenv artifacts: each job uploaded
      // dist/<stack>.env; load them into the environment of the steps below.
      ...needs.map((n) => ({
        name: `Download ${n.stack.id} outputs`,
        uses: 'actions/download-artifact@v8',
        with: { name: outputsArtifact(n), path: 'dist' },
      })),
      ...(needs.length
        ? [{
          name: 'Load the outputs it takes',
          run: needs.map((n) => `cat "dist/${n.stack.id}.env" >> "$GITHUB_ENV"`).join('\n'),
        }]
        : []),
      credentials(j.stack.region),
      {
        name: `Deploy ${j.stack.id}`,
        run: 'npx projen deploy:stack',
        env: { ...variables, ...Object.fromEntries(Object.entries(j.env).map(([k, v]) => [k, render(v, GITHUB)])) },
      },
      {
        name: 'Upload its outputs',
        uses: 'actions/upload-artifact@v7',
        with: { 'name': outputsArtifact(j), 'path': `dist/${j.stack.id}.env`, 'retention-days': 1, 'if-no-files-found': 'error' },
      },
    ]);
  }

  if (opts.pruneAssets !== false) {
    job(PRUNE_JOB.github, [UPLOAD_JOB.github, ...opts.jobs.map((j) => j.githubJob)], [
      ...setup,
      credentials(opts.awsRegion),
      { name: 'Remove old deploy prefixes', run: 'npx projen deploy:prune' },
    ]);
  }
}
