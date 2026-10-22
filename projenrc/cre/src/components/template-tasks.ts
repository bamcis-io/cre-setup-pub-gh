import type { awscdk } from 'projen';
import { CRE_SCRIPTS } from '../paths';

/**
 * `npx projen update-template` — pull the template clone, refresh this package (projenrc/cre/), the
 * construct library and the skill links from it, then re-synth and build. See scripts/template/update-template.sh.
 */
export function createTemplateTasks(project: awscdk.AwsCdkTypeScriptApp): void {
  project.addTask('update-template', {
    description:
      'Pull the cre-demo-template clone (recorded in projenrc/cre/SOURCE.json, else the sibling; --from <path>), refresh projenrc/cre, cre-constructs and the skill links from it, then synth + build.',
    steps: [{ exec: `bash ${CRE_SCRIPTS}/template/update-template.sh "$@"`, receiveArgs: true }],
  });
}
