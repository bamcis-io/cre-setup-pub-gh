import { createFailureScenarioChecks } from './components/tooling-tasks';
import { CreDemoTemplateApp } from './cre-demo-template-app';
import type { CreDemoTemplateAppOptions } from './cre-demo-template-app';
import { FAILURE_SCENARIO_LAYOUT, STANDARD_LAYOUT } from './layout';

/**
 * A demo of a product, solution or pattern: a reference for what good looks like, built fast on
 * the vetted constructs and the provided CI/CD. Layout `src/{cdk,app}`.
 */
export class CreResilienceDemo extends CreDemoTemplateApp {
  constructor(options: CreDemoTemplateAppOptions) {
    super(options, STANDARD_LAYOUT, 'resilience-demo.md');
  }
}

/**
 * A system with an intentional failure, used to benchmark a resilience agent. The agent reviews
 * only `src/clean`; `src/deploy` overlays the failure injection, tooling and observability.
 */
export type CreFailureScenarioOptions = CreDemoTemplateAppOptions & {
  /**
   * The scenario's own give-away terms for `test:clean-purity`, on top of the generic list: the
   * source system's names, internal class names, incident ids, commit SHAs, years. Extended regexes,
   * case-insensitive.
   */
  readonly forbiddenTerms?: string[];
  /** The answer key (JSON Lines). @default `ground-truth/<name>.jsonl` */
  readonly groundTruthFile?: string;
};

export class CreFailureScenario extends CreDemoTemplateApp {
  public readonly groundTruthFile: string;

  constructor(options: CreFailureScenarioOptions) {
    super(options, FAILURE_SCENARIO_LAYOUT, 'failure-scenario.md');
    this.groundTruthFile = options.groundTruthFile ?? `ground-truth/${options.name}.jsonl`;
    createFailureScenarioChecks(this, {
      forbiddenTerms: options.forbiddenTerms ?? [],
      groundTruthFile: this.groundTruthFile,
    });
  }
}

/**
 * A recreation of a customer's environment from transcripts, notes or diagrams: a sandbox to
 * experiment in. The deployed environment is the deliverable. Layout `src/{cdk,app}`.
 */
export class CreCustomerSandbox extends CreDemoTemplateApp {
  constructor(options: CreDemoTemplateAppOptions) {
    super(options, STANDARD_LAYOUT, 'customer-sandbox.md');
  }
}
