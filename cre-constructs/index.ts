/**
 * The `@cre/constructs` package entry point: re-exports every family. Projects import from here
 * (`import { RegionalNetwork, CreEdgeStack } from '@cre/constructs'`), never from a family path.
 */
export * from './core/index.js'; // CreApp, CreStack, CreIamStack, creSynthesizer, requireDeployRegion
export * from './networking/index.js'; // RegionalNetwork, ParameterizedVpc, network stacks, PeeringMesh
export * from './auth/index.js'; // allowed-cidr, cloudfront-signer, federate-alb, cognito
export * from './observability/index.js'; // DemoObservability, CreDashboardStack, DEMO_METRIC_NAMESPACE
export * from './load-generation/index.js'; // LoadGenerator
export * from './failure-injection/index.js'; // FailureInjectionParameter, InjectApi, FisNetworkExperiments
export * from './dns/index.js'; // ValidatedCertificate, CreDnsStack, PrivateApiCustomDomain
