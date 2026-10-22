import type { awscdk } from 'projen';
import { CRE_SCRIPTS } from '../paths';
import type { PlannedStack } from './stack-plan';

/**
 * The deploy's projen tasks, generated from the project's stacks (the CDK app, read into cre-stacks.json,
 * resolved by stack-plan.ts). The CI pipelines run one job per stack (stack-jobs.ts) and call:
 *
 *   deploy:upload  publish templates and assets to `<ASSETS_BUCKET_PREFIX>-<region>` for every
 *                  listed region (deploy:s3), and image tarballs to ECR in the regions of the
 *                  stacks that use them (deploy:docker, with containerImages)
 *   deploy:stack   deploy one stack with the variables its job sets (scripts/build/deploy-stack.sh)
 *   deploy:prune   remove old asset prefixes (keeps the current one and two before it)
 *
 * `deploy` is the same pipeline on a machine (scripts/build/deploy-local.py): deploy:upload, then
 * deploy:stack once per stack in deploy order with exactly its CI job's variables (outputs from the
 * dotenv files the stacks it needs wrote), then deploy:prune. Never CloudFormation exports, never
 * matching by name.
 */
export interface DeployTaskOptions {
  /**
   * Registers `deploy:docker` (crane pushes the image tarballs from dist/content.zip to ECR in the
   * regions of the stacks that use each image) and folds it into `deploy:upload`. Both providers.
   */
  readonly containerImages?: boolean;
  /** Default for the `BINDLE_ID` deploy env (a caller/CI value overrides it). */
  readonly bindleId?: string;
  /** Delete old deploy prefixes after a successful deploy (keeps current + 2). @default true */
  readonly pruneAssets?: boolean;
}

export const DEFAULT_BINDLE_ID = 'amzn1.bindle.resource.v4o66fxvd3zfcqvcmhxq'; // aws-cre

export function createDeployTasks(
  project: awscdk.AwsCdkTypeScriptApp,
  stacks: readonly PlannedStack[],
  opts: DeployTaskOptions = {},
): void {
  const regions = [...new Set(stacks.map((s) => s.region))];
  const bucket = (region: string) => `$ASSETS_BUCKET_PREFIX-${region}`;

  // Sync the unpacked dist/content.zip to every region's bucket: one step per region, each naming
  // its region as a literal. Not an env var threaded down from `deploy`: projen forwards a spawn
  // step's env exactly one hop and the publish chain is two (deploy → deploy:upload → deploy:s3),
  // so a threaded region publishes everything to the ambient region's bucket. Three demos shipped
  // that bug; test/deploy-tasks.test.ts EXECUTES this tree to keep it fixed.
  const deployS3 = project.addTask('deploy:s3', {
    description: "Sync dist/content.zip contents to every listed region's assets bucket.",
    requiredEnv: ['ASSETS_BUCKET_PREFIX', 'ASSETS_PREFIX'],
    // No stacks (a fresh scaffold): no region, so nothing to check, unpack or sync.
    steps: regions.length === 0 ? [{ say: 'No stacks in the app yet: nothing to upload.' }] : [
      { say: 'Checking every region has its assets bucket...' },
      // A missing bucket and missing credentials both fail head-bucket: say which (#16).
      ...regions.map((r) => ({
        exec: `out=$(aws s3api head-bucket --region "${r}" --bucket "${bucket(r)}" 2>&1) || { case "$out" in ` +
          `*"(404)"*|*"Not Found"*|*NoSuchBucket*) echo "Assets bucket ${bucket(r)} does not exist: run npx projen setup:aws for ${r}." >&2 ;; ` +
          `*"(403)"*|*Forbidden*) echo "Assets bucket ${bucket(r)} is not readable with these AWS credentials ($(aws sts get-caller-identity --query Arn --output text 2>&1)): the runner role needs s3:ListBucket on it, or the bucket is another account's." >&2 ;; ` +
          `*) echo "Could not check assets bucket ${bucket(r)}: $out (AWS credentials missing or expired?)" >&2 ;; esac; exit 1; }`,
        shell: ['bash', '-c'],
      })),
      { say: 'Unpacking dist/content.zip...' },
      { exec: 'rm -rf dist/content && mkdir -p dist/content && unzip -q dist/content.zip -d dist/content' },
      // --region is explicit so we don't rely on the CLI's auto-discovery HEAD call (which some
      // scoped IAM policies don't permit). containers/ go to ECR, not S3.
      ...regions.map((r) => ({
        exec: [
          `echo "Syncing dist/content/ → s3://${bucket(r)}/$ASSETS_PREFIX";`,
          `aws s3 sync --no-progress --region "${r}" --exclude "containers/*" dist/content "s3://${bucket(r)}/$ASSETS_PREFIX"`,
        ].join(' '),
      })),
    ],
  });

  const deployUpload = project.addTask('deploy:upload', {
    description: 'Publish build artifacts: S3 content, and container images with containerImages.',
  });
  deployUpload.spawn(deployS3);

  if (opts.containerImages) {
    // Each image goes to the regions of the stacks whose asset manifests list it (container-plan
    // records which stacks use each image; the list says where each stack goes).
    const stackRegions = stacks.map((s) => `${s.id}=${s.region}`).join(' ');
    const deployDocker = project.addTask('deploy:docker', {
      description: 'Push pre-built image tarballs to ECR via crane, in the regions of the stacks that use them.',
      steps: [{ exec: `bash ${CRE_SCRIPTS}/build/deploy-docker.sh --stack-regions "${stackRegions}"` }],
    });
    deployUpload.spawn(deployDocker);
  }

  // AwsCdkTypeScriptApp ships a `deploy` task that runs `cdk deploy`. Templates go to AWS via
  // `aws cloudformation` against an uploaded S3 prefix instead, so replace it (gotcha 8).
  project.tasks.removeTask('deploy');

  // One stack: what every CI stack job runs. Its variables come from the job (stack-jobs.ts);
  // deploy-stack.sh checks them, assumes ASSUME_ROLE when set, and deploys. It resolves nothing.
  project.addTask('deploy:stack', {
    description: 'Deploy one stack with the variables its pipeline job sets (see cre-stacks.json `jobs`).',
    steps: [{ exec: `bash ${CRE_SCRIPTS}/build/deploy-stack.sh` }],
  });

  if (opts.pruneAssets !== false) {
    project.addTask('deploy:prune', {
      description: 'Remove old deploy prefixes from every region bucket (keeps current + 2).',
      requiredEnv: ['ASSETS_BUCKET_PREFIX', 'ASSETS_PREFIX'],
      steps: [{ exec: `PRUNE_BUCKETS="${regions.map(bucket).join(' ')}" bash ${CRE_SCRIPTS}/build/prune-assets.sh` }],
    });
  }

  // The pipeline on your machine: deploy-local.py does what the CI runner does. It reads the jobs
  // from cre-stacks.json, gets every variable they need (environment, .cre/deploy.env, then a
  // prompt), and runs deploy:upload, deploy:stack once per stack in deploy order with exactly that
  // job's variables (and the outputs of the stacks it needs), then deploy:prune.
  project.addTask('deploy', {
    description: 'Run the deploy pipeline locally (builds first): deploy:upload, deploy:stack per stack in order, deploy:prune. [--stack id] [--no-build] [--profile p]',
    steps: [{ exec: `python3 ${CRE_SCRIPTS}/build/deploy-local.py "$@"`, receiveArgs: true }],
  });

  // projen's cdk tasks that act on AWS around the pipeline: `destroy` (cdk destroy, unaware of the
  // assets prefix and the cross-account DNS stack) and `diff` (cdk diff against the bootstrap-less
  // synthesizer), and `clobber` (git reset --hard + clean -fdx: deletes uncommitted work). Replaced
  // by `stacks`, `teardown` (#29, #30).
  for (const name of ['destroy', 'diff', 'clobber']) project.tasks.removeTask(name);
  const stacksPy = (cmd: string) => [{ exec: `python3 ${CRE_SCRIPTS}/build/stacks.py ${cmd} "$@"`, receiveArgs: true }];
  project.addTask('stacks', {
    description: "Each planned stack's deployed status, plus orphans (this project's stacks no longer in the plan). [--profile p]",
    steps: stacksPy('status'),
  });
  project.addTask('stacks:outputs', {
    description: "The deployed stacks' outputs (URLs, ids) as `<stack> <Key>=<Value>`. [--stack id] [--profile p]",
    steps: stacksPy('outputs'),
  });
  project.addTask('teardown', {
    description: 'Delete the stacks in reverse deploy order (prints first; --yes deletes). [--orphans | --stack id] [--profile p]',
    steps: stacksPy('teardown'),
  });
}
