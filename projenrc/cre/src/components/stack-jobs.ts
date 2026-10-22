import type { PlannedStack } from './stack-plan';

/**
 * One deploy job per stack, generated from the stacks the CDK app records. This module is the single
 * definition of what each job gets; the GitLab pipeline, the GitHub workflow and the local
 * `npx projen deploy` all render the same {@link StackJob}, so every stack job everywhere has the
 * same variables, built into the pipeline. `deploy:stack` (scripts/build/deploy-stack.sh) resolves
 * nothing: it deploys what the job hands it.
 *
 * Every stack job has exactly these variables:
 *   STACK_NAME, TEMPLATE_NAME   the stack's deployed name
 *   AWS_REGION, AWS_DEFAULT_REGION  its region (a literal)
 *   ASSETS_BUCKET               `<ASSETS_BUCKET_PREFIX>-<region>`
 *   OUTPUTS_PREFIX              its outputs go to dist/<stack>.env as `<PREFIX>_<OUTPUT>`
 *   OUTPUTS                     the outputs written there: the ones later stacks take
 *   ROLE_ARN                    CloudFormation's service role (its `executionRole`)
 *   ASSUME_ROLE                 the role assumed first (its `assumeRole`), or empty
 *   STACK_PARAMETERS            `Key=value` lines, values referencing the variables and the
 *                               earlier stacks' outputs directly
 *   REQUIRED_VARS               variables that must be non-empty (checked before deploying)
 *   REQUIRED_OUTPUTS            `<VAR>=<stack>/<Output>` for each output it takes, so an output
 *                               the earlier stack didn't produce stops the job, naming both
 * plus each deploy variable the stack uses, by its own name.
 *
 * Outputs travel the way each runner moves job data: GitLab dotenv artifacts from the jobs in
 * `needs`, GitHub artifacts loaded into `$GITHUB_ENV`, and locally the dotenv files themselves.
 */

/** A value in a job's environment, before it is written for a particular runner. */
export type JobValue =
  | { readonly kind: 'literal'; readonly value: string }
  /** A deploy variable (project CI/CD variable, GitHub secret, or local env). */
  | { readonly kind: 'var'; readonly name: string; readonly optional: boolean; readonly fallback?: string }
  /** An earlier stack's output, as the dotenv variable its job wrote. */
  | { readonly kind: 'output'; readonly variable: string }
  /** Parts joined with no separator, e.g. the bucket name. */
  | { readonly kind: 'concat'; readonly parts: readonly JobValue[] }
  /** `Key=value` lines (STACK_PARAMETERS). */
  | { readonly kind: 'lines'; readonly lines: readonly (readonly [string, JobValue])[] };

export interface StackJob {
  readonly stack: PlannedStack;
  /** GitLab job name: `deploy:<stack name>`. */
  readonly gitlabJob: string;
  /** GitHub job id: `deploy-<stack name>`. */
  readonly githubJob: string;
  /** The stack jobs it needs, in list order. */
  readonly needs: readonly PlannedStack[];
  /** The variables, always the same keys in the same order (see the module comment). */
  readonly env: Readonly<Record<string, JobValue>>;
  /** The deploy variables it uses, by name (passed to the job under their own names). */
  readonly variables: readonly string[];
}

/**
 * Names the deploy sets itself: every stack job's variables, the pipeline-wide ones, and the
 * re-exec guard. A deploy variable (CreCfnParameter `fromVariable`, `assumeRole`, `executionRole`,
 * a project CI/CD variable or GitHub secret) can't use one: on GitLab a project variable outranks
 * the job's own, so it would silently replace the generated value. Setup refuses the same names
 * (`RESERVED_VARS` in scripts/setup/crelib.py; a test keeps the two lists equal).
 */
export const RESERVED_VARIABLES: readonly string[] = [
  'STACK_NAME', 'TEMPLATE_NAME', 'AWS_REGION', 'AWS_DEFAULT_REGION', 'ASSETS_BUCKET', 'OUTPUTS_PREFIX',
  'OUTPUTS', 'ROLE_ARN', 'ASSUME_ROLE', 'STACK_PARAMETERS', 'REQUIRED_VARS', 'REQUIRED_OUTPUTS',
  'ASSETS_PREFIX', 'PROJECT_NAME', 'CRE_ROLE_ASSUMED',
];

/** The prefix of the dotenv variables carrying stack outputs (`STACK_<ID>_<OUTPUT>`), also reserved. */
export const RESERVED_PREFIX = 'STACK_';

/** Why a deploy variable name is reserved, or undefined if it isn't. */
export function reservedVariable(name: string): string | undefined {
  if (RESERVED_VARIABLES.includes(name)) return 'the deploy sets it for every stack job';
  if (name.startsWith(RESERVED_PREFIX)) return `${RESERVED_PREFIX}* names carry earlier stacks' outputs`;
  return undefined;
}

export const UPLOAD_JOB = { gitlab: 'deploy:upload', github: 'deploy-upload' } as const;
export const PRUNE_JOB = { gitlab: 'deploy:prune', github: 'deploy-prune' } as const;

/** Values the deploy supplies when the variable is unset or empty. */
export interface JobDefaults {
  readonly [variable: string]: string;
}

const lit = (value: string): JobValue => ({ kind: 'literal', value });

/** The dotenv variable a stack's output is written under. */
export function outputVariable(stack: PlannedStack, output: string): string {
  return `${stack.outputsPrefix}_${output.toUpperCase()}`;
}

export function stackJobs(planned: readonly PlannedStack[], defaults: JobDefaults): StackJob[] {
  const byId = new Map(planned.map((p) => [p.id, p]));
  return planned.map((s) => {
    const variables = new Set<string>(['ASSETS_BUCKET_PREFIX']);
    const required = new Set<string>(['ASSETS_BUCKET_PREFIX']);
    const requiredOutputs: string[] = [];
    const v = (name: string, optional: boolean): JobValue => {
      variables.add(name);
      const fallback = defaults[name];
      if (!optional && fallback === undefined) required.add(name);
      return { kind: 'var', name, optional: optional || fallback !== undefined, fallback };
    };
    const lines = Object.entries(s.parameters).map(([key, src]): [string, JobValue] => {
      if (src.kind === 'literal') return [key, lit(src.value)];
      if (src.kind === 'env') return [key, v(src.variable, src.optional)];
      const from = byId.get(src.stack)!;
      const variable = outputVariable(from, src.output);
      requiredOutputs.push(`${variable}=${from.id}/${src.output}`);
      return [key, { kind: 'output', variable }];
    });
    // An optional execution role that is empty falls back to the default one.
    const roleArn: JobValue = s.executionRole.optional
      ? (variables.add('CFN_EXECUTION_ROLE_ARN'), required.add('CFN_EXECUTION_ROLE_ARN'),
      { ...v(s.executionRole.variable, true), fallback: 'CFN_EXECUTION_ROLE_ARN' } as JobValue)
      : v(s.executionRole.variable, false);
    const assumeRole: JobValue = s.assumeRole ? v(s.assumeRole.variable, s.assumeRole.optional) : lit('');
    const env: Record<string, JobValue> = {
      STACK_NAME: lit(s.id),
      TEMPLATE_NAME: lit(s.id),
      AWS_REGION: lit(s.region),
      AWS_DEFAULT_REGION: lit(s.region),
      ASSETS_BUCKET: { kind: 'concat', parts: [v('ASSETS_BUCKET_PREFIX', false), lit(`-${s.region}`)] },
      OUTPUTS_PREFIX: lit(s.outputsPrefix),
      OUTPUTS: lit(s.outputs.join(' ')),
      ROLE_ARN: roleArn,
      ASSUME_ROLE: assumeRole,
      STACK_PARAMETERS: { kind: 'lines', lines },
      REQUIRED_VARS: lit([...required].join(' ')),
      REQUIRED_OUTPUTS: lit(requiredOutputs.join(' ')),
    };
    return {
      stack: s,
      gitlabJob: `deploy:${s.id}`,
      githubJob: `deploy-${s.id}`,
      needs: s.needs.map((n) => byId.get(n)!),
      env,
      variables: [...variables],
    };
  });
}

/**
 * Write a value for one runner.
 *   - `var`:    how that runner references a deploy variable (GitLab `$X`, GitHub
 *               `${{ secrets.X }}`, bash `${X}`), with its fallback where it has one;
 *   - `output`: how it references a dotenv variable loaded from an earlier job;
 *   - `literal`: escaped so the runner expands nothing in it.
 */
export interface Dialect {
  literal(v: string): string;
  variable(name: string, optional: boolean, fallback?: string, isVariable?: boolean): string;
  output(variable: string): string;
}

export function render(value: JobValue, d: Dialect): string {
  switch (value.kind) {
    case 'literal': return d.literal(value.value);
    case 'var': return d.variable(value.name, value.optional, value.fallback);
    case 'output': return d.output(value.variable);
    case 'concat': return value.parts.map((p) => render(p, d)).join('');
    case 'lines': return value.lines.map(([k, x]) => `${k}=${render(x, d)}`).join('\n');
  }
}

/** Fallbacks are either a default value or another variable's name (CFN_EXECUTION_ROLE_ARN). */
const isVariableName = (f: string) => /^[A-Z_][A-Z0-9_]*$/.test(f);

/**
 * GitLab job `variables:`. An undefined `$X` expands to empty, and a `needs` job's dotenv values
 * expand inside variables (both checked on a live pipeline). GitLab has no `${X:-y}`: a default
 * value is set once as a pipeline variable (project variables override it), and an empty optional
 * ROLE_ARN falls back to CFN_EXECUTION_ROLE_ARN in deploy-stack.sh.
 */
export const GITLAB: Dialect = {
  literal: (v) => v.replace(/\$/g, '$$$$'),
  variable: (name) => `$${name}`,
  output: (variable) => `$${variable}`,
};

/** GitHub job `env:`. Variables are secrets; outputs were loaded into `env` by an earlier step. */
export const GITHUB: Dialect = {
  literal: (v) => v,
  variable: (name, _optional, fallback) => {
    if (fallback === undefined) return `\${{ secrets.${name} }}`;
    const f = isVariableName(fallback) ? `secrets.${fallback}` : `'${fallback.replace(/'/g, "''")}'`;
    return `\${{ secrets.${name} || ${f} }}`;
  },
  output: (variable) => `\${{ env.${variable} }}`,
};

/** Bash, inside double quotes (the local deploy). */
export const BASH: Dialect = {
  literal: (v) => v.replace(/([\\"$`])/g, '\\$1'),
  variable: (name, optional, fallback) => {
    if (fallback !== undefined) return `\${${name}:-${isVariableName(fallback) ? `\${${fallback}:-}` : BASH.literal(fallback)}}`;
    return optional ? `\${${name}:-}` : `\${${name}:-}`;
  },
  output: (variable) => `\${${variable}:-}`,
};

/** The plan's record of a job, as written to cre-stacks.json (GitLab's rendering, for reading). */
export function jobRecord(j: StackJob): Record<string, unknown> {
  return {
    gitlab: j.gitlabJob,
    github: j.githubJob,
    needs: j.needs.map((n) => n.id),
    variables: j.variables,
    env: Object.fromEntries(Object.entries(j.env).map(([k, x]) => [k, render(x, GITLAB)])),
  };
}

/**
 * The job's variables as data (cre-stacks.json `jobs.<stack>.spec`): what the local deploy renders,
 * the same way a CI runner expands the job's variables.
 */
export function jobSpec(j: StackJob): Record<string, unknown> {
  return {
    needs: j.needs.map((n) => n.id),
    variables: j.variables,
    env: j.env,
  };
}
