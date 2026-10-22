// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Release workflow customization.
 *
 * Carried (structure-for-structure) from
 * multi-az-workshop/projenrc/workflows/release-workflow.ts. It customizes
 * projen's NATIVE `release.yml` (emitted because `release: true`) rather than
 * authoring a new workflow.
 *
 * Generalizations from the multi-az source:
 *   - the path filter triggering a release is the green-skeleton's source tree
 *     (`src/**`, `.projenrc.ts`, `projenrc/**`) instead of multi-az's
 *     `content/**` / `static/**` / `contentspec.yaml` (which do not exist here).
 *   - the release asset attached is `dist/content.zip` (the artifact the shared
 *     build tasks produce), same as multi-az.
 *   - the release_github `release` step is overridden (prerelease detection +
 *     idempotent `gh release create`). Step positions are looked up by step id
 *     (stepIndex below), not hard-coded, so a projen bump that reorders the native
 *     steps fails synth loudly instead of overriding the wrong step.
 *
 * Why the merged-PR trigger needs more than `on.pull_request`: projen's native
 * release.yml assumes a push to a branch. On `pull_request: closed`, GITHUB_REF is
 * `refs/pull/<n>/merge`, which GitHub deletes once the PR closes. So the native
 * checkout, the "Check for new commits" step (which asks the remote for $GITHUB_REF's
 * tip — empty, so upload + publish were skipped) and `gh release create --target
 * $GITHUB_REF` all broke, and no GitHub Release was ever published. Each is re-pointed
 * at the merge commit / base branch below.
 */

import { Component } from 'projen';
import type { AwsCdkTypeScriptApp } from 'projen/lib/awscdk';
import type { GithubWorkflow } from 'projen/lib/github';

// The commit being released: the merge commit for a merged PR, main's tip for a manual
// dispatch.
const RELEASE_SHA = '(github.event.pull_request.merge_commit_sha || github.sha)';
// The branch whose tip must still be that commit for the release to proceed.
const RELEASE_BRANCH_REF =
  "(github.event_name == 'pull_request' && format('refs/heads/{0}', github.event.pull_request.base.ref) || github.ref)";

/**
 * Rendered index of the step with `id` in `job`, failing synth if projen's native layout
 * changed. A job's `tools` (e.g. release_github's `node`) are rendered as one setup step
 * each, PREPENDED to `steps` — so they shift every index in the YAML without appearing
 * in the job definition.
 */
function stepIndex(workflow: GithubWorkflow, job: string, id: string): number {
  const def = workflow.getJob(job) as { steps?: { id?: string }[]; tools?: Record<string, unknown> } | undefined;
  const i = (def?.steps ?? []).findIndex((s) => s.id === id);
  if (i < 0) {
    throw new Error(`release workflow: no step '${id}' in job '${job}' — projen's native release.yml changed; re-check customizeReleaseWorkflow`);
  }
  return Object.keys(def?.tools ?? {}).length + i;
}

/**
 * Customizes the native release workflow.
 * @param project The AwsCdkTypeScriptApp project instance.
 */
export function customizeReleaseWorkflow(project: AwsCdkTypeScriptApp): void {
  const releaseWorkflow = project.github?.tryFindWorkflow('release');

  if (!releaseWorkflow) {
    console.warn('Release workflow not found. Make sure release is enabled in project configuration.');
    return;
  }

  if (releaseWorkflow.file) {
    // Run releases on merged PRs that touch the demo's source, not on every push.
    releaseWorkflow.file.addDeletionOverride('on.push');
    releaseWorkflow.file.addOverride('on.pull_request', {
      types: ['closed'],
      branches: ['main'],
      paths: [
        'src/**',
        'projenrc/**',
        '.projenrc.ts',
      ],
    });

    // Only release on an actual merge (not a closed-without-merge PR), or on a manual
    // dispatch — which this condition used to skip, dry runs included.
    releaseWorkflow.file.addOverride(
      'jobs.release.if',
      "github.event_name == 'workflow_dispatch' || github.event.pull_request.merged == true",
    );

    // Step positions only exist once projen's Release component has added its jobs
    // (in its own preSynthesize), so the per-step overrides are applied from a
    // component registered after it — its preSynthesize runs later.
    const file = releaseWorkflow.file;
    new (class extends Component {
      // Explicit so the call type-checks even where projen is unresolved (the template's
      // own skeleton.projenrc-wiring test), which would otherwise infer a 0-arg constructor.
      constructor(scope: AwsCdkTypeScriptApp) {
        super(scope);
      }
      public override preSynthesize(): void {
        overrideReleaseSteps(releaseWorkflow, file);
      }
    })(project);
  }
}

/** Per-step overrides of the native release.yml (see the file header for why). */
function overrideReleaseSteps(releaseWorkflow: GithubWorkflow, file: NonNullable<GithubWorkflow['file']>): void {
  {
    // Check out the merge commit, not the (deleted) refs/pull/<n>/merge.
    const checkout = stepIndex(releaseWorkflow, 'release', 'checkout');
    file.addOverride(`jobs.release.steps.${checkout}.with.ref`, `\${{ ${RELEASE_SHA} }}`);

    // "Is the commit we built still the branch tip?" — ask about the base branch...
    const remote = stepIndex(releaseWorkflow, 'release', 'git_remote');
    file.addOverride(`jobs.release.steps.${remote}.env.GITHUB_REF`, `\${{ ${RELEASE_BRANCH_REF} }}`);
    // ...and compare its tip with the commit we built, not github.sha.
    for (const id of ['backup_artifact_permissions', 'upload_artifact']) {
      const i = stepIndex(releaseWorkflow, 'release', id);
      file.addOverride(`jobs.release.steps.${i}.if`, `\${{ steps.git_remote.outputs.latest_commit == ${RELEASE_SHA} }}`);
    }
    file.addOverride(
      'jobs.release_github.if',
      `needs.release.outputs.tag_exists != 'true' && needs.release.outputs.latest_commit == ${RELEASE_SHA}`,
    );

    // Override the final release step to attach dist/content.zip and handle
    // prerelease detection + idempotency (re-running on an existing tag is a
    // no-op rather than a failure). --target must be a branch or commit SHA;
    // $GITHUB_REF (refs/pull/<n>/merge) is neither once the PR has merged.
    const release = stepIndex(releaseWorkflow, 'release_github', 'release');
    file.addOverride(`jobs.release_github.steps.${release}.env.RELEASE_SHA`, `\${{ ${RELEASE_SHA} }}`);
    file.addOverride(`jobs.release_github.steps.${release}.run`, [
      'errout=$(mktemp)',
      'RELEASE_TAG=$(cat dist/releasetag.txt)',
      '',
      '# Check if this is a prerelease by looking for hyphen in tag (e.g., v1.0.0-alpha)',
      'if [[ "$RELEASE_TAG" == *"-"* ]]; then',
      '  echo "Creating prerelease: $RELEASE_TAG"',
      '  gh release create "$RELEASE_TAG" \\',
      '    --title "$RELEASE_TAG" \\',
      '    --prerelease \\',
      '    -R "$GITHUB_REPOSITORY" \\',
      '    -F dist/changelog.md \\',
      '    --target "$RELEASE_SHA" \\',
      '    dist/content.zip 2> "$errout" && true',
      'else',
      '  echo "Creating release: $RELEASE_TAG"',
      '  gh release create "$RELEASE_TAG" \\',
      '    --title "$RELEASE_TAG" \\',
      '    -R "$GITHUB_REPOSITORY" \\',
      '    -F dist/changelog.md \\',
      '    --target "$RELEASE_SHA" \\',
      '    dist/content.zip 2> "$errout" && true',
      'fi',
      '',
      'exitcode=$?',
      'if [ $exitcode -ne 0 ] && ! grep -q "Release.tag_name already exists" "$errout"; then',
      '  cat "$errout"',
      '  exit $exitcode',
      'fi',
    ].join('\n'));
  }
}
