import { CreResilienceDemo } from './projenrc/cre/src';

// Everything the template pins (toolchain, tsconfig/eslint, CI/CD for the provider below, build,
// deploy and git tasks, steering) lives in the project type, vendored at projenrc/cre/. Keep this
// file to the demo's own choices. To pick up template fixes: npx projen update-template.
const project = new CreResilienceDemo({
  name: 'cre-setup-pub-gh',
  description: 'Private-to-public test of the CRE GitHub setup',
  repository: 'https://github.com/bamcis-io/cre-setup-pub-gh',
  github: { username: 'hakenmt', awsRegion: 'us-east-1', appSlug: 'aws-cre-automation-mhaken' },

  // The stacks, and the pipeline that deploys them, come from the CDK app: every top-level stack is
  // a CreStack with its region, and every parameter the pipeline passes is a CreCfnParameter.
  // `npx projen` synthesizes the app and generates the pipeline from it. Guide: cre-pipelines.md.
  // containerImages: true,            // build + push every DockerImageAsset (gates tasks AND CI jobs)
  // bindleId: 'amzn1.bindle.resource.…', // CloudFront Signer bindle (default: aws-cre)
});

project.synth();
