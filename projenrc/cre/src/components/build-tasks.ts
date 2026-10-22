import type { awscdk } from 'projen';
import { CRE_SCRIPTS } from '../paths';

/**
 * Provider-AGNOSTIC build lifecycle for the green CRE demo skeleton.
 *
 * Part of the cre-projen package (vendored into each demo at projenrc/cre/), called by
 * CreDemoTemplateApp for both providers. CI in both families only ever invokes
 * the projen tasks registered here (`npx projen <task>`) — this is the PROJEN_PATTERN
 * "CI ↔ task parity" principle that keeps the GitHub/GitLab seam narrow.
 *
 * The lifecycle:
 *
 *   preCompileTask   → wipe assets/ + cdk.out/ so downstream phases start clean
 *   compileTask      → tsc (projen-native, unchanged)
 *   postCompileTask  → `synth:silent` (cdk synth), check:stack-wiring, check:stack-plan, then
 *                      with containerImages build:container-plan + build:docker
 *   testTask         → jest + eslint (projen-native, unchanged)
 *   packageTask      → stage CFN assets (package:assets) → zip into dist/content.zip
 *
 * Result: one `yarn build` (or `npx projen build`) produces `dist/content.zip`
 * carrying the CFN template + staged file assets the deploy mechanism consumes.
 *
 * The container tasks exist only with `containerImages`: without a DockerImageAsset in the app
 * there is nothing for them to build.
 */
/**
 * Options for {@link createBuildTasks}.
 *
 * `containerImages` is the single toggle gating the container-build
 * lifecycle. When false (the green skeleton default), NO docker tasks are registered
 * and NO docker steps attach to postCompile — the skeleton emits zero container work
 * and stays green with zero edits (NFR1). The provider workflow factories receive the
 * SAME flag so CI jobs and lifecycle tasks light up together — never one without the
 * other (the failure mode where a job is emitted with no asset, or vice-versa).
 *
 * Both providers build images the same way: `build:container-plan` + `build:docker` run in
 * postCompile and write `assets/containers/<hash>.tar.gz` (kaniko: natively on GitLab's kaniko
 * job image, docker-wrapped wherever a daemon exists, i.e. GitHub runners and laptops), and
 * `deploy:docker` pushes them with crane. See workflows/container-tools.ts.
 */
export interface BuildTaskOptions {
  readonly containerImages?: boolean;
  /**
   * The deployed name of every stack (from the app, via cre-stacks.json). Each one's template and
   * assets go into dist/content.zip; taken from the same plan the deploy uses, so the two can't disagree.
   */
  readonly stackIds: string[];
}

export function createBuildTasks(
  project: awscdk.AwsCdkTypeScriptApp,
  opts: BuildTaskOptions,
): void {
  // --- granular tasks (also wired into the lifecycle below) ----------------

  // For each top-level stack, scripts/build/package.py:
  //   1. Rewrites nested templates that reference ${AssetsBucket*} to accept
  //      them as parameters, piping the parent's params through.
  //   2. Copies every file asset into assets/<hash>.<ext> (zipping directories).
  //   3. Copies the top-level template to assets/<stack_name>.json so the deploy
  //      task can target it by name via --template-url.
  //
  // Does NOT wipe assets/ — preCompile handles that once per build so postCompile
  // phases can share the folder.
  const packageAssets = project.addTask('package:assets', {
    description: "Stage every stack's assets into assets/ and rewrite nested templates",
    steps: [
      { exec: `chmod +x ${CRE_SCRIPTS}/build/package.py` },
      ...opts.stackIds.map((id) => ({
        // $PROJECT_NAME (no braces): projen's built-in dax shell expands $VAR
        // but passes the braced ${VAR} form through literally.
        exec: `${CRE_SCRIPTS}/build/package.py ${id} .`,
      })),
    ],
  });

  const packageContent = project.addTask('package:content', {
    description: 'Build dist/content.zip — the artifact the deploy stage consumes',
    steps: [
      { exec: 'rm -f dist/content.zip' },
      // An app with no stacks stages nothing, and zip refuses an empty tree ("Nothing to do!"):
      // write an empty archive, so the artifact the deploy stage consumes always exists.
      {
        // bash, not projen's built-in shell, which has no `if`.
        exec: 'mkdir -p dist assets && cd assets && if [ -n "$(ls -A)" ]; then zip -r ../dist/content.zip .; ' +
          'else python3 -c "import zipfile; zipfile.ZipFile(\'../dist/content.zip\', \'w\').close()"; fi',
        shell: ['bash', '-c'],
      },
    ],
  });

  // projen's synth:silent is `cdk synth -q`, which exits 1 for an app with no stacks. A fresh
  // scaffold has none on purpose, so synth through synth.sh, which accepts exactly that case.
  project.tasks.tryFind('synth:silent')?.reset(`bash ${CRE_SCRIPTS}/build/synth.sh`);

  // CI build entry point — the single composite task BOTH providers' build jobs
  // invoke (GitHub `build.yml`'s build step; GitLab `build:cdk`). Named so a
  // workflow `needs:`/job graph can reference it directly. Runs the non-docker
  // half of projen's lifecycle so the build job needs only a plain node image:
  // wipe assets/ + cdk.out/, compile, synth, lint + unit test (projen's `test`
  // task spawns both jest and eslint), then stage CFN assets into assets/.
  project.addTask('ci:build:cdk', {
    description: 'CI: CDK synthesis + tests + asset staging (provider build-job entry point)',
    steps: [
      // Wipe assets/ + cdk.out/ because `cdk synth` doesn't prune stale
      // *.assets.json files from renamed/removed stacks, and package:assets
      // expects a clean assets/ tree. dist/ is NOT wiped — it carries state
      // between lifecycle phases (dist/assets_prefix, dist/<stack>.env dotenvs,
      // dist/<stack>.params.json). `mkdir -p dist` is defensive.
      { exec: 'rm -rf assets cdk.out && mkdir -p assets dist' },
      // Before anything regenerates them: the committed pipeline must be the current one (#15).
      { spawn: 'check:generated' },
      { spawn: 'compile' },
      { spawn: 'synth:silent' },
      { spawn: 'check:stack-wiring' },
      { spawn: 'check:stack-plan' },
      { spawn: 'check:deploy-prereqs' },
      { spawn: 'test' },
      { spawn: 'package:assets' },
    ],
  });

  // CI package entry point — zips the staged assets/ tree into dist/content.zip.
  // Kept as a named `ci:build:*` alias (rather than the job calling
  // `yarn package:content` directly) so all build-stage CI entry points share
  // the namespace and the provider workflow's job→task mapping stays trivial.
  project.addTask('ci:build:package', {
    description: 'CI: zip assets/ into dist/content.zip (provider package-job entry point)',
    steps: [{ spawn: 'package:content' }],
  });

  // Stacks pass values as outputs -> parameters of the same name, never CloudFormation exports
  // (steering cre-pipelines.md). Checked on what synth wrote, so it runs straight after synth:
  // in post-compile (local `npx projen build`, before any image build) and in ci:build:cdk.
  // Not in `test`: `npx projen test` alone doesn't synth, so it would check a stale cdk.out.
  project.addTask('check:generated', {
    description: 'CI only: fail when the committed generated files (pipeline, cre-stacks.json, ...) are not what npx projen makes',
    steps: [{ exec: `bash ${CRE_SCRIPTS}/build/check-generated.sh` }],
  });

  const checkWiring = project.addTask('check:stack-wiring', {
    description: 'Fail if any synthesized stack uses a CloudFormation export/import (pass outputs -> parameters instead)',
    steps: [{ exec: `python3 ${CRE_SCRIPTS}/build/check-stack-wiring.py cdk.out` }],
  });
  project.postCompileTask.spawn(checkWiring);
  // The app must still create exactly the stacks the committed cre-stacks.json (and the pipeline) was generated from, each
  // a CreStack with no fixed environment, declaring every parameter the deploy passes and every
  // output later stacks use: the deploy is generated from the list, so a mismatch fails here.
  const checkPlan = project.addTask('check:stack-plan', {
    description: 'Fail if the synthesized stacks differ from the stacks list in .projenrc.ts (names, environments, parameters, outputs)',
    steps: [{ exec: `python3 ${CRE_SCRIPTS}/build/check-stack-plan.py cre-stacks.json cdk.out` }],
  });
  project.postCompileTask.spawn(checkPlan);
  // The stacks can deploy with what setup created (#26): their regions have buckets, their
  // variables were set, secrets are masked. Offline (cre-setup.json), so a gap fails the MR's
  // build instead of the deploy after the merge.
  const checkPrereqs = project.addTask('check:deploy-prereqs', {
    description: 'Fail if a stack needs a region bucket or CI/CD variable setup never made (cre-setup.json); prints the setup command',
    steps: [{ exec: `python3 ${CRE_SCRIPTS}/build/check-deploy-prereqs.py cre-stacks.json cre-setup.json cdk.out` }],
  });
  project.postCompileTask.spawn(checkPrereqs);

  // --- container tasks (gated on containerImages) ---------------------------
  //
  // Registered ONLY when containerImages is true. These are the provider-agnostic
  // task layer behind both providers' container jobs:
  //   build:container-plan  reads cdk.out/*.assets.json (the one DockerImageAsset the
  //                         LoadGenerator declares) → assets/containers/manifest.json
  //                         + dist/container-plan.tsv (scripts/build/container-plan.sh).
  //   build:docker          builds each image into assets/containers/<hash>.tar.gz via
  //                         kaniko (scripts/build/build-docker.sh; POSIX sh, no python).
  //   ci:build:container-plan  composite alias the GitLab build:container-plan job calls.
  // postCompile ordering matters: plan THEN build (build reads the .tsv the plan wrote).
  // container-plan.sh + build-docker.sh ship in every demo (projenrc/cre/scripts/build/),
  // but these tasks must still never register when the flag is off (NFR1 — a
  // build:docker that runs with no Dockerfile breaks green).
  if (opts.containerImages) {
    const buildContainerPlan = project.addTask('build:container-plan', {
      description: 'Enumerate DockerImageAssets into manifest.json + container-plan.tsv',
      steps: [{ exec: `bash ${CRE_SCRIPTS}/build/container-plan.sh` }],
    });
    const buildDocker = project.addTask('build:docker', {
      description: 'Build every DockerImageAsset into assets/containers/ (kaniko)',
      steps: [{ exec: `sh ${CRE_SCRIPTS}/build/build-docker.sh` }],
    });
    project.addTask('ci:build:container-plan', {
      description: 'CI: produce manifest.json + container-plan.tsv (container-plan job entry point)',
      steps: [{ spawn: 'build:container-plan' }],
    });
    // postCompile: synth:silent (projen-native) already ran cdk synth above; now plan
    // the containers, then build them. Local `npx projen build` builds the image via
    // build-docker.sh's docker-wrapped kaniko fallback when a docker daemon is present.
    project.postCompileTask.spawn(buildContainerPlan);
    project.postCompileTask.spawn(buildDocker);
  }

  // --- lifecycle wiring ----------------------------------------------------

  // preCompile: clean slate. Downstream phases share assets/ and cdk.out/, so
  // wipe both once here rather than at the start of every individual task.
  // Wiping cdk.out/ matters because `cdk synth` doesn't prune files from stacks
  // that have been renamed or removed. dist/ is NOT wiped (it carries cross-phase
  // state); `mkdir -p dist` just ensures it exists for package:content to write.
  project.preCompileTask.exec('rm -rf assets cdk.out && mkdir -p assets dist');


  // package: stage CFN assets, then zip the whole assets/ tree. Gotcha 8 —
  // reset() before re-authoring the native `package` task so we replace projen's
  // default rather than appending to it. package:assets runs here (not earlier)
  // because rewriting templates for the ${AssetsBucket*} params is a packaging
  // concern, not a compile concern.
  project.packageTask.reset();
  project.packageTask.description = 'Stage CFN assets and zip into dist/content.zip';
  project.packageTask.spawn(packageAssets);
  project.packageTask.spawn(packageContent);
}
