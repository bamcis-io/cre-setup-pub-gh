---
inclusion: always
---
# CRE construct library (`@cre/constructs`): what exists, when to use it, how to import it

Every construct below is already installed in this project as the package `@cre/constructs` (the
`cre-constructs/` directory, a `link:` dependency). Before writing a construct or a stack, check
this list. Use one when it fits; write your own only when none does, and say why in the design.

## How to use it

```ts
import { Aws } from 'aws-cdk-lib';
import { CreApp, CreCfnParameter, CreStack, RegionalNetwork, LoadGenerator, CreEdgeStack } from '@cre/constructs';
```

- Import from the package name only, never from a path into `cre-constructs/`.
- Treat it like a package in `node_modules`: **never edit `cre-constructs/`**. `npx projen test`
  fails if it differs from what the template installed. A change goes to
  `cre-demo-template/constructs/` via an MR there; `npx projen update-template` then installs the
  new version here (new constructs arrive, removed ones go).
- The project's own constructs and stacks live in `src/` (FailureScenario: `src/clean` or
  `src/deploy`) and import `@cre/constructs` like any other code.
- Props, examples and **mandatory gotchas** for each construct are in
  `cre-demo-template/constructs/README.md` (sections named below), and in the TSDoc on each class.
- Stacks: subclass or instantiate the `Cre*Stack` classes. `app.ts` creates a `CreApp`; every
  top-level stack extends `CreStack`, is named `app.stackId('<name>')`, and gets its deploy `region`
  as a prop. Every parameter the pipeline passes is a `CreCfnParameter` (`cre-pipelines.md`): the
  pipeline is generated from the app.
- **No `env`, no region or account literals.** Every template is environment-agnostic (CreStack
  refuses `env`); the `region` prop says where the pipeline deploys it. Inside a stack, refer to where it is
  deployed only through `Aws.REGION`, `Aws.ACCOUNT_ID`, `Aws.PARTITION`, `Aws.URL_SUFFIX`
  (`import { Aws } from 'aws-cdk-lib'`) in template strings:
  `` `arn:${Aws.PARTITION}:ssm:${Aws.REGION}:${Aws.ACCOUNT_ID}:parameter/x` ``. That is the only
  spelling: not `cdk.Aws.*`, `Fn.sub('${AWS::Region}')`, `Fn.ref('AWS::AccountId')` or
  `Stack.of(this).region`. Never a `'us-east-1'` string, `arn:aws:`, or an `amazonaws.com` endpoint
  (service principals excepted): the same
  templates must deploy in any region and partition (commercial, GovCloud, China, EUSC). A stack
  that must be in one region asserts it with `requireDeployRegion` (a CloudFormation Rule on
  `AWS::Region`).

## The deploy shape these fit into

The pipeline is one deploy job per `CreStack`, in dependency order (`cre-pipelines.md`). The stacks
below already wire themselves: give them the objects they take values from (`CreEdgeStack` the
routes' `CreEdgeOrigin`s, `CreDashboardStack` the `edge`, `CrePeeringStack` the `networks`,
`addPrivateApi` the regions' `PrivateApiCustomDomain`s) and their `CreCfnParameter`s add the outputs
there and the deploy order.

## Families

### core
`CreApp` (the app: strips all CDK metadata from every template, records the stacks the pipeline is
generated from, `stackId()`, and fails the synth on a top-level stack that isn't a CreStack or a
parameter without a default or a source), `CreStack` (base class for every top-level stack: its
`region`, `dependsOn`, `assumeRole`, `executionRole`; stack name = id; the deploy's S3-prefix
synthesizer; the `AssetsBucketName`/`AssetsBucketPrefix` parameters every deploy passes; no `env`),
`CreCfnParameter` (a parameter with its source: `from` another stack's value, or `fromVariable`),
`creSynthesizer()`, `requireDeployRegion()` (a deploy-time Rule that the stack is in one of the given
regions), and `CreIamStack` (the shared IAM stack, refused outside its partition's IAM control-plane
region, us-east-1 in `aws`). **When:** every project: one `CreApp`, every stack a `CreStack`, every
passed parameter a `CreCfnParameter`. Roles shared by stacks in several regions go in a
`CreIamStack`; the stacks that use them take the ARNs with `CreCfnParameter({ from })`.

### networking
`RegionalNetwork` (the VPC; CIDR from a `VpcCidr` parameter; `ipMode` `ipv4` | `dualstack` |
`ipv6`; S3 and DynamoDB gateway endpoints always), with two standard shapes:
`RegionalNetwork.privateNetwork()` (isolated subnets plus an attached, unrouted internet gateway;
no internet) and `RegionalNetwork.egressOnly()` (outbound internet through a NAT gateway per AZ for
IPv4 and an egress-only internet gateway for IPv6). As stacks: `CrePrivateNetworkStack`,
`CreEgressOnlyNetworkStack` (base `CreNetworkStack`), which own the `VpcCidr` parameter (default the
`vpcCidr` prop) and output what peering needs. Also `ParameterizedVpc` (the building block), `PeeringMesh` and
`CrePeeringStack` (cross-region peering, IPv4 only). **When:** always for the VPC. Default to the
private network (Locust tasks and internal ALBs behind the edge live there; add the interface
endpoints the workload needs, e.g. ECR, ECR_DOCKER, CLOUDWATCH_LOGS for Fargate). Use egress-only
only when the workload calls the internet (NAT bills per hour per AZ and per GB). Choose `ipv6`
only after checking every service supports IPv6-only subnets (README §1). Multi-region means a
network stack per region (`app.stackId('network-<region>')`, each with its `region` and `vpcCidr`)
and a `CrePeeringStack` given them as `networks`, which takes each one's VPC id, CIDR, route tables
and region. README §1.

### observability
`DemoObservability` (dashboard header, a client-availability graph over `ClientSuccess`/
`ClientError`, one `NOT_BREACHING` alarm, a dashboard-URL output), `CreDashboardStack` (that as the
`$PROJECT_NAME-dashboard` stack; given `edge`, its header links the edge and Locust UI and it
deploys after the edge), `DEMO_METRIC_NAMESPACE`, and the `emf/` Python helper for emitting metrics.
**When:** every resilience demo (required). The scaffold has no stacks, so add one:
`new CreDashboardStack(app, app.stackId('dashboard'), { region, projectName: app.stackId() })`.
On its own it is only a shell: design the widgets the project's story depends on
(`dashboardStack.observability.addMetricRow(...)`). README §2.

### auth/cloudfront-signer — the default front door
`CreEdgeStack` (the `$PROJECT_NAME-edge` stack: ONE CloudFront distribution behind CloudFront
Signer, Midway + bindle, with path routes to internal ALBs through VPC origins; deployed to
us-east-1 whatever the app region; commercial `aws` partition only), `CreEdgeOrigin` (in the app stack: opens an internal ALB to
CloudFront and outputs it for the edge stack, by route name), `LOCUST_EDGE_ROUTE`, and
`DistributionSigner` (the signer itself, applied by `CreEdgeStack`). **When:** any UI, operator page
or API a person reaches, including the Locust UI. Steps: a VPC with an internet gateway attached (`RegionalNetwork.privateNetwork()`, or
`includeInternetGateway: true`),
a `CreEdgeOrigin` per ALB (Locust's is automatic: `loadGen.edgeOrigin`), and
`new CreEdgeStack(app, app.stackId('edge'), { region: 'us-east-1', routes: [{ ...LOCUST_EDGE_ROUTE, origin: loadGen.edgeOrigin! }] })`:
each route takes its origin's ALB, and `BindleId` comes from BINDLE_ID. Each origin must serve under its route's path prefix;
CloudFront doesn't rewrite paths. VPC origins are IPv4 or dual-stack (a route's `ipAddressType`),
never IPv6-only. After the first deploy, run `npx projen cfs:onboard` once (it
onboards the `EdgeDomainName` output at CloudFrontSigner); until then every request is 403. Pick another auth family only when this doesn't fit, recorded as a
deviation. README §6.

### auth/federate-alb
`FederateAlbAuth`: Midway through Amazon Federate OIDC at an ALB listener, plus
`app-helper/oidc-data.{ts,py}` to validate `x-amzn-oidc-data` in the app. **When:** an internal web
app on its own ALB hostname with a certificate, when CloudFront doesn't fit. Needs a Federate
service profile. README §7.

### auth/allowed-cidr
`AllowedCidrSecurityGroup`: ingress gated by an `AllowedCidr` parameter, default deny-all, reset
on every deploy. **When:** a non-HTTP endpoint, or a machine caller that can't do Midway; defense
in depth under another mechanism. README §3.

### auth/cognito
`CognitoAuthConstruct` plus `CognitoAlbAuthConstruct` (ALB login) or `CognitoApiGwAuthConstruct`
(HTTP API JWT). **When:** only when non-Amazon users must log in. README §3.

### dns
`ValidatedCertificate({ projectName, domain, hostedZoneId, dnsRoleArn })`: an ACM certificate for
`*.<domain>` validated with no manual step, whether the zone is in this account or another. **When:**
`cre-setup.json` has a `domain` and something serves HTTPS on a custom hostname. One per project per
region. Its stack declares `CreCfnParameter`s with `fromVariable: 'DOMAIN'`, `'HOSTED_ZONE_ID'` and
`'DNS_ROLE_ARN'` (`optional: true`) and passes their values. Records go in a `CreDnsStack`, always in
the partition's Route 53 control-plane region (us-east-1 in `aws`; refused elsewhere, and its Rule
`DnsControlPlaneRegion` refuses others at deploy), with `assumeRole: '$DNS_ROLE_ARN?',
executionRole: '$DNS_CFN_EXECUTION_ROLE_ARN?'` (deployed into the zone's account when those are set). `PrivateApiCustomDomain` (in each region's API stack) + `CreDnsStack.addPrivateApi`
(`PrivateApiDnsRecords`) give a PRIVATE REST API your own hostnames (`api-<region>.<domain>`, and
`api.<domain>` weighted across regions) through its execute-api VPC endpoint: give
`addPrivateApi({ domains })` each region's `PrivateApiCustomDomain` and the records stack takes their
endpoints. **When:** a private API that clients in another (peered) VPC or region
call by name, or that fails over by DNS. Don't hand-build it: README §8 lists seven mistakes that
each cost a 403 or a failed deploy (dotted region names, V1 DomainName, no domain policy,
`aws:SourceVpce` on the API policy, zonal alias target, no access association, dash-form URLs).
README §8.

### load-generation
`LoadGenerator`: Locust on Fargate, one EMF line per request into `DEMO_METRIC_NAMESPACE`.
`exposeWebUi: true` runs the Locust UI on an internal ALB under `/locust` and puts it on the edge
automatically (route `locust`; `edgeRoute: false` keeps it internal). `users` / `spawnRate` set the
run (headless) or the start form's defaults (UI). Scale with `workers: N` (a controller plus N worker
tasks; `processesPerTask` per core), never `desiredCount` with the UI. `addWebPage(path, html)`
adds an operator page at `/locust<path>` (`{{BASE_PATH}}` for links), with `@route` handlers from
`cre_pages` in the locustfile and `addToRolePolicy` for their AWS access; no hand-made Flask routes
(Locust's base path doesn't apply to them). For a story where the backend slows down, hold the
request rate steady with the open-loop pattern (README §4: `constant_throughput` + fire-and-forget
`gevent` in a bounded pool, `nofileUlimit`); closed-loop users slow down with the target and hide
the failure. **When:** the story needs
traffic (most projects). Set `containerImages: true` in `.projenrc.ts`. FailureScenario: only in
`src/deploy`. Drive it headlessly with the `locust-cfs` skill (`LOCUST_URL` = the `EdgeLocustUrl`
output). README §4.

### failure-injection
`FailureInjectionParameter` (the default: an SSM error-rate knob the app reads, plus
`inject-failure.sh` / `restore.sh` and an IAM-authenticated `InjectApi`) and `FisNetworkExperiments`
(opt-in per-AZ FIS latency, packet-loss and CPU-stress templates; `stopConditionAlarm` required). **When:** the story shows a failure and recovery. FailureScenario:
only in `src/deploy`, never in `src/clean`. README §5.
