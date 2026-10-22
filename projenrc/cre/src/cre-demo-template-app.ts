import * as fs from 'fs';
import * as path from 'path';
import { awscdk, javascript } from 'projen';
import { GithubCredentials } from 'projen/lib/github';
import { AppPermission } from 'projen/lib/github/workflows-model';
import { NodePackageManager, UpgradeDependenciesSchedule } from 'projen/lib/javascript';
import { createBuildTasks } from './components/build-tasks';
import { createDeployTasks, DEFAULT_BINDLE_ID } from './components/deploy-tasks';
import { createGitTasks } from './components/git-tasks';
import type { GitProvider } from './components/git-tasks';
import { createSkillsTasks } from './components/skills-tasks';
import { stackJobs } from './components/stack-jobs';
import { planStacks, PLAN_FILE, readPlan, writePlanFile } from './components/stack-plan';
import type { CrePartition } from './components/stack-plan';
import { createSteering } from './components/steering';
import { createTemplateTasks } from './components/template-tasks';
import { createToolingTasks } from './components/tooling-tasks';
import type { ProjectLayout } from './layout';
import { CRE_CONSTRUCTS_DIR, CRE_CONSTRUCTS_PACKAGE, CRE_SCRIPTS } from './paths';
import { GITHUB_QEMU_STEP } from './workflows/container-tools';
import { createDeployWorkflow } from './workflows/deploy-workflow';
import { createGitlabWorkflow } from './workflows/gitlab-workflow';
import { customizeReleaseWorkflow } from './workflows/release-workflow';

/** The projen version the package is built and tested against. Bootstrap installs exactly this. */
export const PROJEN_VERSION = '0.104.8';

export interface GitlabOptions {
  /** Target region: the pipeline's AWS_REGION (assets bucket suffix + AWS_DEFAULT_REGION). */
  readonly awsRegion: string;
}

export interface GithubOptions {
  /** The builder's GitHub username, trusted by projen's auto-approve and auto-queue workflows. */
  readonly username: string;
  /** Region for the deploy workflow's upload and prune jobs; each stack job uses its stack's region. */
  readonly awsRegion: string;
  /**
   * Other people whose PRs auto-approve and auto-queue act on, as for `username` (teammates who push
   * to this repo). Everyone else's PRs wait for a human approval. GitHub logins.
   * @default []
   */
  readonly trustedUsers?: string[];
  /**
   * The builder's GitHub App (`aws-cre-automation-<alias>`, made by `npx projen setup:github-app`)
   * that projen's workflows push, open PRs and merge with (dependency upgrades, self-mutation,
   * auto-merge): its slug, as in `https://github.com/apps/<slug>`. Its token is minted per run,
   * lasts an hour and reaches only the repositories it is installed on; its PRs are authored by
   * `<slug>[bot]`, which auto-approve trusts. The repo needs the `PROJEN_APP_ID` and
   * `PROJEN_APP_PRIVATE_KEY` secrets (setup:repo sets them). The workflow's own GITHUB_TOKEN can't
   * do this job: PRs and merges made with it start no workflows, so the build would never run on an
   * upgrade PR and a merge would never deploy.
   */
  readonly appSlug: string;
}

/** Who opens the PRs auto-approve and auto-queue act on: the builder, the App's bot, GitHub's bots. */
/** The Conventional Commits v1.0.0 types (commit messages and PR titles). */
export const CONVENTIONAL_TYPES = ['feat', 'fix', 'docs', 'style', 'refactor', 'perf', 'test', 'build', 'ci', 'chore', 'revert'] as const;

const GITHUB_LOGIN = /^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$/;

export function trustedAuthors(github: GithubOptions): string[] {
  const people = [github.username, ...(github.trustedUsers ?? [])];
  for (const u of people) {
    // A typo here leaves every PR of that person waiting for an approval nobody gives (#35c).
    if (!GITHUB_LOGIN.test(u ?? '')) throw new Error(`github: '${u}' is not a GitHub login.`);
  }
  return [...new Set(people), `${github.appSlug}[bot]`, 'github-actions[bot]', 'dependabot[bot]'];
}

/**
 * The username in .projenrc.ts against the login setup read from gh (cre-setup.json
 * githubUsername): a hand edit that differs is most likely a typo, and auto-approve would then trust
 * nobody's PRs.
 */
export function checkGithubUsername(github: GithubOptions, setupFile: string): string | undefined {
  let recorded: string | undefined;
  try {
    recorded = JSON.parse(fs.readFileSync(setupFile, 'utf-8')).githubUsername;
  } catch {
    return undefined;
  }
  if (recorded && recorded.toLowerCase() !== github.username.toLowerCase() &&
    !(github.trustedUsers ?? []).some((u) => u.toLowerCase() === recorded!.toLowerCase())) {
    return `cre: github.username '${github.username}' isn't the GitHub login setup recorded ('${recorded}', cre-setup.json): ` +
      'auto-approve trusts only the logins in .projenrc.ts. Fix the typo, or add the other login to github.trustedUsers.';
  }
  return undefined;
}

/**
 * The credential projen's workflows push, open PRs and merge with. The App token is limited to what
 * those jobs do: write the repository (including workflow files, which upgrades and self-mutation
 * regenerate) and pull requests.
 */
export function projenCredentials(github: GithubOptions): GithubCredentials {
  if (!/^[a-z0-9][a-z0-9-]*$/.test(github.appSlug ?? '')) {
    throw new Error(`github.appSlug '${github.appSlug}' is not an App slug (the <slug> in https://github.com/apps/<slug>).`);
  }
  return GithubCredentials.fromApp({
    permissions: { contents: AppPermission.WRITE, pullRequests: AppPermission.WRITE, workflows: AppPermission.WRITE },
  });
}

/**
 * How a GitHub PR waits for its build before it merges:
 *  - `workflow`: the build workflow merges it (a `merge` job after a passing build). For a private
 *    repo, which a free plan can't protect, so GitHub's own auto-merge would merge at once (seen
 *    live: PR #7 merged before its build ran, and the deploy ran untested).
 *  - `auto-merge`: GitHub's auto-merge, which main's branch protection (setup: the `build` check,
 *    one approval) makes wait for the build. For a public (or internal) repo.
 * From cre-setup.json's `githubVisibility` (setup records it; `npx projen setup:github-public`
 * changes it); private when unknown, the safe default (it merges only after a build either way).
 */
export type GithubMergeMode = 'workflow' | 'auto-merge';

export function githubMergeMode(setupFile: string): GithubMergeMode {
  return githubVisibility(setupFile) === 'private' ? 'workflow' : 'auto-merge';
}

/** cre-setup.json's githubVisibility; 'private' (the safe mode) when it isn't recorded, with a warning. */
export function githubVisibility(setupFile: string): 'private' | 'public' | 'internal' {
  let v: unknown;
  try {
    v = JSON.parse(fs.readFileSync(setupFile, 'utf-8')).githubVisibility;
  } catch {
    return 'private';
  }
  if (v === 'private' || v === 'public' || v === 'internal') return v;
  console.warn(`cre: cre-setup.json has no githubVisibility${v === undefined ? '' : ` ('${String(v)}' isn't private, public or internal)`}, ` +
    'so PRs merge as a private repo\'s do (from the build workflow). Record it: npx projen setup:repo prepare');
  return 'private';
}

/**
 * projen's auto-approve runs on every `opened` and `labeled` event while the PR has the label: a PR
 * opened with both labels fired three runs and three approvals (three review emails to its author).
 * Approve on `opened` (labels already there), on adding the `auto-approve` label itself, and on a
 * new commit (synchronize); never on adding another label. The git tasks open the PR without
 * labels and then add both, so it is approved exactly once.
 */
export function approveOncePerLabel(project: awscdk.AwsCdkTypeScriptApp, github: GithubOptions): void {
  const file = project.github?.tryFindWorkflow('auto-approve')?.file;
  if (!file) return;
  const authors = trustedAuthors(github).map((u) => `github.event.pull_request.user.login == '${u}'`).join(' || ');
  file.addOverride('jobs.approve.if', "contains(github.event.pull_request.labels.*.name, 'auto-approve') && " +
    "(github.event.action != 'labeled' || github.event.label.name == 'auto-approve') && " + `(${authors})`);
}

/**
 * The `workflow` merge mode: a job after a passing build merges the PR with the App's token (so the
 * merge to main starts the deploy). It reads the PR fresh rather than trusting the event: still
 * open, labelled `auto-merge` (the git tasks add the labels just after opening it), a trusted
 * author, from this repository, and its head is still the commit that was built
 * (`--match-head-commit`); otherwise it skips. A newer commit's own build merges that one.
 */
export function addBuildMergeJob(project: awscdk.AwsCdkTypeScriptApp, github: GithubOptions): void {
  const wf = project.github?.tryFindWorkflow('build');
  if (!wf) throw new Error('cre: no build workflow to add the merge job to.');
  const trusted = trustedAuthors(github).join(' ');
  wf.addJob('merge', {
    needs: ['build'],
    runsOn: ['ubuntu-latest'],
    permissions: {},
    if: "github.event_name == 'pull_request' && needs.build.result == 'success' && " +
      '!needs.build.outputs.self_mutation_happened && github.event.pull_request.head.repo.full_name == github.repository',
    steps: [
      {
        name: 'Generate token',
        id: 'generate_token',
        uses: 'actions/create-github-app-token@v3.2.0',
        with: {
          'app-id': '${{ secrets.PROJEN_APP_ID }}',
          'private-key': '${{ secrets.PROJEN_APP_PRIVATE_KEY }}',
          'permission-contents': 'write',
          'permission-pull-requests': 'write',
        },
      },
      {
        name: 'Merge the PR (its build passed)',
        env: {
          GH_TOKEN: '${{ steps.generate_token.outputs.token }}',
          GH_REPO: '${{ github.repository }}',
          PR_NUMBER: '${{ github.event.pull_request.number }}',
          HEAD_SHA: '${{ github.event.pull_request.head.sha }}',
          TRUSTED: trusted,
        },
        run: [
          'set -euo pipefail',
          // A failed read fails the job (set -e), never a silent skip.
          'info=$(gh pr view "$PR_NUMBER" --repo "$GH_REPO" --json state,isDraft,author,headRefOid,labels ' +
            '--jq \'[.state, .isDraft, .author.login, .headRefOid, ([.labels[].name] | join(","))] | @tsv\')',
          'IFS=$\'\\t\' read -r state draft author head labels <<<"$info"',
          // gh reports an App author as app/<slug>; the trusted list names it <slug>[bot].
          'case "$author" in app/*) author="${author#app/}[bot]" ;; esac',
          'skip() { echo "Not merging: $1."; exit 0; }',
          '[ "$state" = OPEN ] || skip "the PR is $state"',
          '[ "$draft" = false ] || skip "the PR is a draft"',
          'case ",$labels," in *,auto-merge,*) ;; *) skip "no auto-merge label" ;; esac',
          'case " $TRUSTED " in *" $author "*) ;; *) skip "$author is not a trusted author" ;; esac',
          '[ "$head" = "$HEAD_SHA" ] || skip "the PR moved on to $head; its own build merges it"',
          'gh pr merge "$PR_NUMBER" --repo "$GH_REPO" --squash --match-head-commit "$HEAD_SHA"',
        ].join('\n'),
      },
    ],
  });
}

/** Exactly one provider. Each carries only its own settings. */
export type ProviderOptions =
  | { readonly gitlab: GitlabOptions; readonly github?: never }
  | { readonly github: GithubOptions; readonly gitlab?: never };

export interface CreDemoTemplateAppBaseOptions {
  readonly name: string;
  readonly description: string;
  readonly repository: string;
  /** @default 'Apache-2.0' */
  readonly license?: string;
  readonly deps?: string[];
  readonly devDeps?: string[];
  /**
   * Build and push container images (every `DockerImageAsset` in the CDK app). Gates the
   * container tasks AND the CI jobs together, so a job is never emitted without its asset.
   * @default false
   */
  readonly containerImages?: boolean;
  /**
   * The partition every stack deploys in: a stack's region outside it is refused, a CreIamStack /
   * CreDnsStack must be in its IAM / Route 53 control-plane region (us-east-1, us-gov-west-1,
   * cn-north-1 / cn-northwest-1, eusc-de-east-1), and a CreEdgeStack (CloudFront) exists only in
   * `aws`. @default 'aws'
   */
  readonly partition?: CrePartition;
  /** Extra .gitignore entries on top of the template's. */
  readonly additionalGitignore?: string[];
  /**
   * Default CloudFront Signer bindle for the deploy env's `BINDLE_ID` (a `BINDLE_ID` CI variable or
   * caller env overrides it). @default the aws-cre bindle
   */
  readonly bindleId?: string;
  /** Delete old deploy prefixes after each successful deploy (keeps current + 2). @default true */
  readonly pruneAssets?: boolean;
}

export type CreDemoTemplateAppOptions = CreDemoTemplateAppBaseOptions & ProviderOptions;

const BASE_GITIGNORE = [
  // .kiro/ steering + skills and .agents/ (PDD's design record) are committed. Only these stay local.
  '.kiro/specs/',
  'idea/',
  // Claude Code per-machine state: agent worktrees (embedded git repos that `--files "."` would
  // commit as gitlinks) and local permissions. `.claude/skills` stays committed.
  '.claude/worktrees/',
  '.claude/settings.local.json',
  '.DS_Store',
  '**/.DS_Store',
  'tsconfig.tsbuildinfo',
  'coverage/',
  'test-reports/',
  'cdk.out*/',
  // Local deploy variables (npx projen deploy reads them; setup writes them).
  '.cre/',
  // Credentials never belong in the repo (npx projen push also refuses them).
  '.env',
  '.env.*',
  '!.env.example',
  '*.pem',
  '*.key',
  '.netrc',
  '.aws/',
  '*.har',
  'cookies.txt',
  'dist/',
  'assets/',
  'tmp/',
  '/lib/',
  '*.d.ts',
  'node_modules/',
  '__pycache__/',
  '*.pyc',
  '.venv/',
  'venv/',
  '*.egg-info/',
];

/** CVE transitive pins (gotcha 11), applied on both providers. */
const RESOLUTIONS = [
  'flatted@>=3.4.2',
  'fast-xml-parser@>=5.7.0',
  'fast-xml-builder@>=1.2.0',
  'fast-uri@>=3.1.2',
  'handlebars@>=4.7.9',
  'picomatch@>=2.3.2',
  'uuid@>=14.0.0',
];

/**
 * The common base for every CRE project type. Owns everything the recipe's MUST-HONOR rules used
 * to ask an agent to keep by hand: the pinned toolchain, tsconfig/eslint shape, the provider's
 * CI/CD, the build/deploy tasks, the git tasks, template updates, and the base steering.
 *
 * Not instantiated directly: use CreResilienceDemo, CreFailureScenario or CreCustomerSandbox.
 */
export abstract class CreDemoTemplateApp extends awscdk.AwsCdkTypeScriptApp {
  public readonly gitProvider: GitProvider;
  public readonly layout: ProjectLayout;

  protected constructor(options: CreDemoTemplateAppOptions, layout: ProjectLayout, typeSteering: string) {
    const isGithub = options.github !== undefined;
    if (isGithub === (options.gitlab !== undefined)) {
      throw new Error('CreDemoTemplateApp: set exactly one of `gitlab` or `github`.');
    }
    const devdirs = [...layout.cdkDirs, 'test', '.projenrc.ts', 'projenrc'];
    const outdirOpt = (options as { outdir?: string }).outdir;
    const mergeMode = githubMergeMode(path.join(outdirOpt ?? '.', 'cre-setup.json'));

    super({
      // Not a public option: tests construct projects in a scratch directory.
      ...((options as { outdir?: string }).outdir ? { outdir: (options as { outdir?: string }).outdir } : {}),
      name: options.name,
      description: options.description,
      repository: options.repository,
      license: options.license ?? 'Apache-2.0',
      deps: [
        // The construct library, installed into ./cre-constructs by the scaffold and by
        // update-template. A link: dependency, so it resolves like any package.
        `${CRE_CONSTRUCTS_PACKAGE}@link:./${CRE_CONSTRUCTS_DIR}`,
        ...(options.deps ?? []),
      ],
      devDeps: ['@types/node', ...(options.devDeps ?? [])],
      peerDeps: [],

      // ---- pinned toolchain (formerly MUST-HONOR rules 1, 2, 12, 13) ----
      defaultReleaseBranch: 'main',
      projenrcTs: true,
      projenVersion: PROJEN_VERSION,
      cdkVersion: '2.248.0',
      cdkVersionPinning: true,
      constructsVersion: '10.5.0',
      typescriptVersion: '~5.6.3',
      appEntrypoint: layout.appEntrypoint,
      srcdir: 'src',
      testdir: 'test',
      sampleCode: false,
      featureFlags: awscdk.CdkFeatureFlags.V2.ALL,
      packageManager: NodePackageManager.YARN_BERRY,
      yarnBerryOptions: {
        yarnRcOptions: { nodeLinker: javascript.YarnNodeLinker.NODE_MODULES },
      },
      tsconfig: { compilerOptions: { isolatedModules: true } },
      tsconfigDev: { compilerOptions: { isolatedModules: true } },
      eslintOptions: {
        dirs: ['src', 'test'],
        devdirs, // formerly MUST-HONOR rule 4
        ignorePatterns: ['*.d.ts', '*.js', 'node_modules/', 'lib/'],
      },
      gitignore: [...BASE_GITIGNORE, ...(options.additionalGitignore ?? [])],

      // ---- provider seam ----
      github: isGithub,
      ...(isGithub
        ? {
          workflowRunsOn: ['ubuntu-24.04-arm'],
          buildWorkflow: true,
          // gotcha 3; QEMU so build:docker can build an image for the other architecture.
          buildWorkflowOptions: { mutableBuild: true, preBuildSteps: options.containerImages ? [GITHUB_QEMU_STEP] : [] },
          release: true,
          dependabot: false,
          depsUpgrade: true,
          depsUpgradeOptions: {
            workflowOptions: {
              labels: ['auto-approve', 'auto-merge'],
              schedule: UpgradeDependenciesSchedule.WEEKLY,
            },
          },
          // A trusted author's PR labelled auto-approve is approved. It merges only after its build
          // passes (githubMergeMode): public repos through auto-queue (GitHub auto-merge, which
          // main's branch protection makes wait for the build), private ones through the build
          // workflow's merge job. The merge deploys (deploy.yml).
          autoApproveOptions: { allowedUsernames: trustedAuthors(options.github!), label: 'auto-approve' },
          githubOptions: {
            mergify: false,
            mergeQueue: mergeMode === 'auto-merge',
            mergeQueueOptions: {
              autoQueueOptions: { allowedUsernames: trustedAuthors(options.github!), labels: ['auto-approve', 'auto-merge'] },
            },
            // gotcha 3: the workflow's own GITHUB_TOKEN can't do this (what it pushes, opens or merges
            // starts no workflow). The builder's GitHub App (GithubOptions.appSlug).
            projenCredentials: projenCredentials(options.github!),
            // Every Conventional Commits type, as the git tasks accept (projen's default is only
            // feat, fix and chore, so a `docs:` or `test:` PR would fail its title check).
            pullRequestLintOptions: { semanticTitleOptions: { types: [...CONVENTIONAL_TYPES] } },
          },
        }
        : {}),
    });

    this.gitProvider = isGithub ? 'github' : 'gitlab';
    this.layout = layout;
    const containerImages = options.containerImages ?? false;
    // The stacks come from the CDK app: a plan-only synth (no bundling, no asset staging) records
    // every CreStack's deploy settings and parameter sources; when the app can't be synthesized yet
    // (fresh clone, no node_modules, a compile error), the committed cre-stacks.json is used.
    // The project name reaches the app as cdk.json context (CreApp.stackId()).
    this.cdkConfig.json.addOverride('context.cre:projectName', this.name);
    if (options.github) {
      const warning = checkGithubUsername(options.github, path.join(this.outdir, 'cre-setup.json'));
      if (warning) console.warn(warning);
    }
    const read = readPlan(this.outdir, this.name);
    if (read.source !== 'synth') {
      const why = read.detail ? ` (${read.detail})` : '';
      console.warn(read.source === 'committed'
        ? `cre: using the committed ${PLAN_FILE}: the CDK app was not synthesized${why}. Run npx projen again once it synthesizes.`
        : `cre: no stacks yet: the CDK app was not synthesized and there is no ${PLAN_FILE}${why}. Run npx projen again after the install.`);
    }
    const stacks = planStacks(this.name, read.stacks, options.partition);
    const stackIds = stacks.map((st) => st.id);
    const deployOpts = { containerImages, bindleId: options.bindleId, pruneAssets: options.pruneAssets };
    // One deploy job per stack: the same variables in GitLab, GitHub and the local deploy.
    const jobDefaults = { BINDLE_ID: options.bindleId ?? DEFAULT_BINDLE_ID };
    const jobs = stackJobs(stacks, jobDefaults);
    // The plan the pipeline was generated from, committed: the record a reviewer sees, the fallback
    // above, and what check:stack-plan compares every synth with.
    writePlanFile(this, read.stacks, stacks, jobs);

    this.package.addPackageResolutions(...RESOLUTIONS);
    this.tasks.addEnvironment('PROJECT_NAME', this.name);

    // `cdk synth` runs via ts-node in CommonJS mode, which can't map the vendored construct
    // barrels' `.js` specifiers to `.ts` sources without the experimental resolver (gotcha 9:
    // set by override, never by editing tsconfig.json).
    this.tryFindObjectFile('tsconfig.json')?.addOverride('ts-node', { experimentalResolver: true });

    if (isGithub) {
      createBuildTasks(this, { containerImages, stackIds });
      createDeployTasks(this, stacks, deployOpts);

      // gotcha 5: projen's AutoQueue ignores labels added after a PR opens unless `labeled` is listed.
      this.github?.tryFindWorkflow('auto-queue')?.file?.addOverride('on.pull_request_target.types', [
        'opened',
        'reopened',
        'ready_for_review',
        'labeled',
      ]);
      approveOncePerLabel(this, options.github!);
      if (mergeMode === 'workflow') addBuildMergeJob(this, options.github!);
      createDeployWorkflow(this.github!, {
        containerImages,
        projectName: this.name,
        awsRegion: options.github!.awsRegion,
        jobs,
        pruneAssets: options.pruneAssets,
      });
      customizeReleaseWorkflow(this);
    } else {
      createBuildTasks(this, { containerImages, stackIds });
      createDeployTasks(this, stacks, deployOpts);
      createGitlabWorkflow(this, {
        containerImages, awsRegion: options.gitlab!.awsRegion, jobs, defaults: jobDefaults, pruneAssets: options.pruneAssets,
      });
    }

    createGitTasks(this, this.gitProvider);
    createTemplateTasks(this);
    createToolingTasks(this, this.gitProvider);
    // @cre/constructs is a dependency: a hand edit to the installed copy fails the build.
    this.testTask.prependExec(`bash ${CRE_SCRIPTS}/template/verify-constructs.sh ${CRE_CONSTRUCTS_DIR}`);
    this.gitattributes.addAttributes(`/${CRE_CONSTRUCTS_DIR}/**`, 'linguist-vendored');
    createSkillsTasks(this);
    createSteering(this, typeSteering, describeLayout(layout));
  }
}

function describeLayout(layout: ProjectLayout): string {
  const lines = [
    `- CDK entrypoint: \`src/${layout.appEntrypoint}\``,
    `- CDK (stacks, constructs): ${layout.cdkDirs.map((d) => `\`${d}/\``).join(', ')}`,
    `- Application logic (code, Dockerfiles, container contexts, assets): ${layout.appDirs.map((d) => `\`${d}/\``).join(', ')}`,
    `- Construct library: \`${CRE_CONSTRUCTS_DIR}/\`, imported as \`${CRE_CONSTRUCTS_PACKAGE}\` (a dependency; never edit it)`,
  ];
  return lines.join('\n');
}
