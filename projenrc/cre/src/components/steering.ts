import * as fs from 'fs';
import * as path from 'path';
import { TextFile } from 'projen';
import type { Project } from 'projen';

/** Steering every project type gets. */
export const BASE_STEERING = ['fidelity.md', 'project-structure.md', 'tooling.md', 'constructs.md', 'pipelines.md'];

/**
 * Emit the package's steering files as projen-managed, read-only files at
 * `.kiro/steering/cre-<name>.md`, so `npx projen update-template` refreshes them and nobody edits
 * them by hand. Demo-specific steering stays as ordinary files next to them.
 *
 * The source markdown is read from `../../steering/` at synth time, i.e. the package's own
 * `steering/` dir (projenrc/cre/steering in a demo).
 */
export function createSteering(project: Project, typeSteering: string, layoutDoc: string): void {
  const dir = path.join(__dirname, '..', '..', 'steering');
  for (const file of [...BASE_STEERING, typeSteering]) {
    let body = fs.readFileSync(path.join(dir, file), 'utf-8');
    if (file === 'project-structure.md') {
      body = body.replace('{{LAYOUT}}', layoutDoc);
    }
    new TextFile(project, `.kiro/steering/cre-${file}`, {
      lines: body.replace(/\n$/, '').split('\n'),
      readonly: true,
    });
  }
}
