import type { awscdk } from 'projen';
import type { GitProvider } from './git-tasks';
import { CRE_SCRIPTS } from '../paths';

/**
 * Tasks every project type gets besides build/deploy/git:
 *
 *   cfs:curl <METHOD> <url|/path> [body]   call anything behind CloudFront Signer (Midway)
 *   cfs:onboard [--domain d] [--status]     onboard the edge distribution at CloudFrontSigner
 *   setup:aws / setup:repo / setup:dns      re-run or repair the account / repo / DNS setup that
 *                                           /cre-demo-init ran (same scripts, same flags)
 */
export function createToolingTasks(project: awscdk.AwsCdkTypeScriptApp, provider: GitProvider): void {
  const quoted = (script: string) => [{ exec: `bash ${CRE_SCRIPTS}/${script} "$@"`, receiveArgs: true }];
  const python = (script: string) => [{ exec: `python3 ${CRE_SCRIPTS}/${script} "$@"`, receiveArgs: true }];
  project.addTask('cfs:curl', {
    description: 'Call a CloudFront Signer (Midway) protected URL: cfs:curl GET https://dxxxx.cloudfront.net/path',
    steps: quoted('cfs/cfs-curl.sh'),
  });
  project.addTask('cfs:onboard', {
    description: 'Onboard the edge distribution at CloudFrontSigner (Midway; once, after the first edge deploy). --status | --offboard',
    steps: quoted('cfs/cfs-onboard.sh'),
  });
  project.addTask('setup:aws', {
    description: 'Assets buckets, CloudFormation execution role and CI runner role in the target account (idempotent).',
    steps: python('setup/aws_account.py'),
  });
  project.addTask('setup:repo', {
    description: `Create/adopt the ${provider === 'gitlab' ? 'GitLab project' : 'GitHub repo'} and set CI variables (prepare | finalize).`,
    steps: python(`setup/repo_${provider}.py`),
  });
  if (provider === 'github') {
    project.addTask('setup:github-public', {
      description: 'A private repo made public, end to end (from a clean main): scan the history for credentials, show what becomes readable; with --yes make it public, protect main, regenerate the workflows for GitHub auto-merge, push them as a PR and wait for it to merge.',
      steps: [{ exec: `python3 ${CRE_SCRIPTS}/setup/repo_github.py publish "$@"`, receiveArgs: true }],
    });
    project.addTask('setup:github-app', {
      description: 'Create (once) or find your CRE automation GitHub App, aws-cre-automation-<alias>, and its key in ~/.cre.',
      steps: python('setup/github_app.py'),
    });
  }
  project.addTask('setup:dns', {
    description: 'External DNS: check the zone, or create the cross-account DNS roles / SuperNova prerequisites.',
    steps: python('setup/dns.py'),
  });
}

/** CreFailureScenario checks, run as part of `test`. */
export function createFailureScenarioChecks(
  project: awscdk.AwsCdkTypeScriptApp,
  opts: { forbiddenTerms: string[]; groundTruthFile: string },
): void {
  const purity = project.addTask('test:clean-purity', {
    description: 'Assert src/clean reads as an ordinary system (no failure, scenario or provenance tells).',
    env: { CRE_FORBIDDEN_TERMS: opts.forbiddenTerms.join('\n') },
    steps: [{ exec: `bash ${CRE_SCRIPTS}/failure-scenario/clean-purity-scan.sh` }],
  });
  // The answer key is written once the scenario exists, so a fresh scaffold (no file yet) passes
  // with a warning; once the file exists it must be valid.
  const gt = project.addTask('test:ground-truth', {
    description: `Validate the answer key (${opts.groundTruthFile}): a gap, a red herring, fixed vocabularies, real paths.`,
    steps: [{
      exec: `if [ -f "${opts.groundTruthFile}" ]; then python3 ${CRE_SCRIPTS}/failure-scenario/ground-truth-check.py "${opts.groundTruthFile}"; else echo "⚠️  ${opts.groundTruthFile} not written yet (required before the scenario is done)"; fi`,
      shell: ['bash', '-c'],
    }],
  });
  project.testTask.spawn(purity);
  project.testTask.spawn(gt);
}
