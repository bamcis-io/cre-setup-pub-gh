// Public surface of the CRE projen package. A demo's .projenrc.ts imports from './projenrc/cre'.
export { CreDemoTemplateApp, PROJEN_VERSION } from './cre-demo-template-app';
export type {
  CreDemoTemplateAppOptions,
  CreDemoTemplateAppBaseOptions,
  GithubOptions,
  GitlabOptions,
  ProviderOptions,
} from './cre-demo-template-app';
export { CreResilienceDemo, CreFailureScenario, CreCustomerSandbox } from './project-types';
export type { CreFailureScenarioOptions } from './project-types';
export type { CrePartition, SynthesizedStack } from './components/stack-plan';
export { DNS_CONTROL_PLANE_REGION, IAM_CONTROL_PLANE_REGION } from './components/stack-plan';
export { DEFAULT_BINDLE_ID } from './components/deploy-tasks';
export type { GitProvider } from './components/git-tasks';
export type { ProjectLayout } from './layout';
export { STANDARD_LAYOUT, FAILURE_SCENARIO_LAYOUT } from './layout';
