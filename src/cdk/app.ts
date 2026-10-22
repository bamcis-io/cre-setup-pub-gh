#!/usr/bin/env node
// The CRE construct library is a dependency (`@cre/constructs`, installed at ./cre-constructs by the
// template). Import everything from the package; .kiro/steering/cre-constructs.md lists what is in
// it and when to use each. Never edit cre-constructs/.
import { CreApp } from '@cre/constructs';

// CreApp: no CDK metadata in any template, every top-level stack must be a CreStack, and it records
// the stacks for the pipeline (`npx projen` generates one deploy job per stack from them).
const app = new CreApp();

// No stacks yet: the project's design decides what it deploys (a VPC, or none at all for a
// serverless design). Each top-level stack is a CreStack with the region the pipeline deploys it
// to, named from the project:
//
//   new MyStack(app, app.stackId(), { region: 'us-east-2' });          // `$PROJECT_NAME`
//   new CreDashboardStack(app, app.stackId('dashboard'), { region: 'us-east-2', projectName: app.stackId() });
//
// Guide: .kiro/steering/cre-pipelines.md. Until a stack exists, the build is green and the deploy
// stage uploads nothing.

app.synth();
