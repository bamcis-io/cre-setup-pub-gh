import * as crypto from 'crypto';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr_assets from 'aws-cdk-lib/aws-ecr-assets';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as ecs_patterns from 'aws-cdk-lib/aws-ecs-patterns';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as servicediscovery from 'aws-cdk-lib/aws-servicediscovery';
import { Construct, IConstruct } from 'constructs';
import { DEMO_METRIC_NAMESPACE, DEMO_METRIC_NAMESPACE_ENV_VAR } from '../observability/metric-namespace.js';
import { CreEdgeOrigin } from '../auth/cloudfront-signer/edge-origin.js';

/**
 * The generic base-container build context vendored alongside this construct
 * (`constructs/src/load-generation/container/`). A demo overlays it via
 * {@link LoadGeneratorProps.workloadDirectory} to drop in its own locustfile + helper
 * modules (merged by `stageOverlay`). The Dockerfile `COPY . .` makes overlays ride
 * along without changing the cross-provider build path.
 *
 * Resolved relative to `__dirname` exactly as the other vendored-asset constructs do
 * (`peering-mesh.ts` / `inject-api.ts`); projen runs `cdk synth` via ts-node in CJS and
 * the library build emits the same `__dirname` reference, so a vendored copy under a
 * demo's `src/cdk/lib/constructs/load-generation/` finds its sibling `container/`.
 */
const DEFAULT_CONTAINER_DIR = path.join(__dirname, 'container');

/** Never copied from an overlay: local tooling state, not workload code. */
const OVERLAY_SKIP = new Set(['__pycache__', '.git', '.venv', 'venv', 'node_modules', '.pytest_cache', 'cdk.out']);

/**
 * Build context for an overlay: the base container with `overlayDir`'s files on top.
 *
 * `DockerImageAsset` builds exactly the directory it is given, so pointing it straight at
 * a demo dir holding only a locustfile + helpers failed the build — no Dockerfile,
 * entrypoint.sh or requirements.txt. Merging here makes `workloadDirectory` the overlay
 * the docs always promised; a file the overlay provides (even the Dockerfile) wins.
 *
 * The staging dir is keyed by the overlay's absolute path and rebuilt on every synth. CDK
 * fingerprints asset CONTENT, not location, and copies the context into cdk.out, so the
 * image hash is stable and CI builds from cdk.out, never from this temp dir.
 */
function stageContext(constructPath: string, overlayDir: string | undefined, pages: Map<string, string>): string {
  const overlay = overlayDir ? path.resolve(overlayDir) : undefined;
  const isBase = !overlay || overlay === path.resolve(DEFAULT_CONTAINER_DIR);
  if (isBase && pages.size === 0) return DEFAULT_CONTAINER_DIR;
  if (overlay && !isBase && (!fs.existsSync(overlay) || !fs.statSync(overlay).isDirectory())) {
    throw new Error(`LoadGenerator: workloadDirectory '${overlayDir}' is not a directory`);
  }
  // Keyed by overlay AND construct path: two generators sharing an overlay can have different pages.
  const key = crypto.createHash('sha256').update(`${overlay ?? ''}\0${constructPath}`).digest('hex').slice(0, 16);
  const staging = path.join(os.tmpdir(), `cre-loadgen-context-${key}`);
  fs.rmSync(staging, { recursive: true, force: true });
  fs.cpSync(DEFAULT_CONTAINER_DIR, staging, { recursive: true });
  if (overlay && !isBase) {
    fs.cpSync(overlay, staging, {
      recursive: true,
      filter: (src) => !OVERLAY_SKIP.has(path.basename(src)),
    });
  }
  if (pages.size > 0) {
    // Read by cre_pages.py at startup: pages.json maps each path to its file.
    const dir = path.join(staging, 'cre_pages');
    fs.mkdirSync(dir, { recursive: true });
    const manifest: Record<string, string> = {};
    [...pages.entries()].sort(([a], [b]) => a.localeCompare(b)).forEach(([pagePath, html], i) => {
      manifest[pagePath] = `page-${i}.html`;
      fs.writeFileSync(path.join(dir, `page-${i}.html`), html);
    });
    fs.writeFileSync(path.join(dir, 'pages.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  }
  return staging;
}

/**
 * Locust's own web UI routes. A page on one of them would be shadowed (or shadow the UI), so
 * addWebPage refuses them and anything under them.
 */
const LOCUST_ROUTES = ['/swarm', '/stop', '/reset', '/stats', '/exceptions', '/tasks', '/logs', '/user', '/assets', '/static', '/login', '/auth', '/update_user'];

/**
 * A ContainerImage whose image is the generator's DockerImageAsset, created when first needed
 * (at prepare time, through an Aspect) rather than in the constructor, so pages added with
 * addWebPage() after construction are part of the build context and of the image hash.
 */
class DeferredAssetImage extends ecs.ContainerImage {
  private readonly roles: iam.IRole[] = [];
  constructor(private readonly resolveAsset: () => ecr_assets.DockerImageAsset) { super(); }
  public bind(_scope: Construct, containerDefinition: ecs.ContainerDefinition): ecs.ContainerImageConfig {
    this.roles.push(containerDefinition.taskDefinition.obtainExecutionRole());
    return { imageName: cdk.Lazy.string({ produce: () => this.resolveAsset().imageUri }) };
  }
  /** Grant every bound execution role pull access once the asset exists. */
  public grantPulls(asset: ecr_assets.DockerImageAsset): void {
    this.roles.forEach((role) => asset.repository.grantPull(role));
  }
}

/**
 * Properties for {@link LoadGenerator}.
 *
 * Target wiring (URL/path/method/extra env), rate controls, Fargate sizing, and architecture
 * are all props; the architecture prop drives BOTH the image platform AND the task
 * `runtimePlatform` so they can never drift (an ARM image on an x86 task fails to start).
 */
export interface LoadGeneratorProps {
  /** VPC to run the Locust Fargate service in. */
  readonly vpc: ec2.IVpc;

  /**
   * Subnet selection for the task. Default PRIVATE_WITH_EGRESS. Demos on a
   * fully-isolated VPC (no NAT) must pass PRIVATE_ISOLATED and provide ECR /
   * ECR_DOCKER / CLOUDWATCH_LOGS VPC endpoints.
   */
  readonly taskSubnets?: ec2.SubnetSelection;

  // ---- target (system-under-test) -----------------------------------------
  /**
   * Primary target base URL, INCLUDING the scheme (`https://...`). A CDK token is fine —
   * e.g. `api.url`, or `` `http://${alb.loadBalancerDnsName}` `` (a bare ALB DNS name has
   * no scheme, so every request would fail). A literal without a scheme throws at synth.
   * A trailing slash is fine: base URL and path are joined without doubling it.
   */
  readonly targetUrl: string;
  /** Path the default task hits. Default '/'. */
  readonly targetPath?: string;
  /** HTTP method the default task uses. Default 'GET'. */
  readonly httpMethod?: string;
  /** Extra env vars injected into the container (multi-endpoint demos). */
  readonly extraTargets?: { [envVarName: string]: string };

  // ---- rate controls (Locust user count x wait time) ----------------------
  /** Locust users (concurrent simulated clients). The run size headless; the start form's default with the web UI. Default 10. */
  readonly users?: number;
  /** Users started per second (the ramp). Headless, the run's ramp; the start form's default with the web UI. Default 10. */
  readonly spawnRate?: number;
  readonly minWaitSeconds?: number;
  readonly maxWaitSeconds?: number;
  readonly requestTimeoutSeconds?: number;

  // ---- observability -------------------------------------------------------
  /**
   * EMF namespace the container emits into. MUST equal the observability
   * dashboard's namespace AND be in the same region as this task's log group.
   * Defaults to the shared {@link DEMO_METRIC_NAMESPACE} constant. C3: the
   * construct injects this into the container under the env var name
   * `DEMO_METRIC_NAMESPACE` (NOT `EMF_NAMESPACE`) so the base locustfile and the
   * observability Python helper read the same variable.
   */
  readonly emfNamespace?: string;
  /**
   * Log group name. Default `/<stack name>/<construct id>/loadgen`: log group names are unique per
   * account and region, so the default must include the stack (a bare `/<id>/loadgen` collided with
   * another project's LoadGenerator in the same account and failed CloudFormation validation).
   */
  readonly logGroupName?: string;
  readonly logRetention?: logs.RetentionDays;

  // ---- Fargate sizing / arch ----------------------------------------------
  readonly cpu?: number;
  readonly memoryLimitMiB?: number;
  /**
   * Number of independent standalone tasks (headless only). Default 1. Must be 1 with
   * `exposeWebUi` or `workers`: the ALB would spread `/swarm`, `/stop` and the stats calls across
   * independent Locusts. To scale, use `workers`.
   */
  readonly desiredCount?: number;

  // ---- distributed load (controller + workers) --------------------------------
  /**
   * Number of Locust WORKER tasks. Default 0: one standalone task generates the load. When `> 0`
   * the main task becomes the Locust controller (Locust's `--master` mode: coordinator and web UI; it runs no users) and this
   * many worker tasks run in a second Fargate service, not behind the ALB. Workers find the controller
   * through a Cloud Map private DNS name and connect to its port 5557 (opened to the workers' SG
   * only). Users are spread across the workers. Headless with workers, the controller waits for all of
   * them before starting. Scale load by adding workers (and `processesPerTask`).
   */
  readonly workers?: number;
  /** vCPU units for each worker task. Default `cpu`. */
  readonly workerCpu?: number;
  /** Memory (MiB) for each worker task. Default `memoryLimitMiB`. */
  readonly workerMemoryLimitMiB?: number;
  /**
   * Locust processes per generating task (`--processes`: each worker, or the standalone task).
   * Locust uses one core per process; set it to the task's vCPU count to use them all. Default 1.
   */
  readonly processesPerTask?: number;
  /**
   * `nofile` (open files) soft and hard ulimit for every Locust container. Raise it for open-loop
   * workloads (fire-and-forget requests at a fixed rate): in-flight connections climb when the
   * target slows, and the default (1024 on Fargate) runs out. Fargate allows up to 1048576.
   * Default: not set.
   */
  readonly nofileUlimit?: number;
  /**
   * Image + task arch. Default ARM64. The image `platform` and the task
   * `runtimePlatform.cpuArchitecture` are BOTH derived from this so they can
   * never drift.
   */
  readonly architecture?: ecs.CpuArchitecture;

  // ---- workload definition -------------------------------------------------
  /**
   * A directory OVERLAID on the vendored base container: its files are
   * added to, or replace, the base context's. Typically just `locustfile.py` + helper
   * modules; the base Dockerfile, entrypoint.sh and requirements.txt are used unless the
   * overlay provides its own. Default: the base container alone.
   */
  readonly workloadDirectory?: string;

  // ---- web UI --------------------------------------------------------------
  /**
   * Expose the Locust web UI behind an internal ALB. Default false (headless,
   * `--headless` run controlled by users/spawnRate). Single-task only — see
   * `desiredCount`; size load with the UI's user count instead.
   */
  readonly exposeWebUi?: boolean;
  /**
   * With `exposeWebUi`: the Locust UI's route on the project's CloudFront Signer edge
   * (`CreEdgeStack`). Locust serves under `basePath` (`--web-base-path`), and a `CreEdgeOrigin`
   * named `name` opens the internal ALB to CloudFront and outputs its ARN and DNS name for the edge
   * stack. `false` leaves the UI on the internal ALB only (reach it from inside the VPC).
   * @default { name: 'locust', basePath: '/locust' }, which matches LOCUST_EDGE_ROUTE
   */
  readonly edgeRoute?: false | { readonly name?: string; readonly basePath?: string };
}

/**
 * On-demand Locust-on-Fargate load generator.
 *
 * ONE {@link ecr_assets.DockerImageAsset} is the single source of truth for the container
 * build: `build:docker` builds it with kaniko (native on GitLab, docker-wrapped on GitHub and
 * locally) and `deploy:docker` pushes it with crane. The Dockerfile carries NO `--platform` — the
 * platform comes from the asset, derived from {@link LoadGeneratorProps.architecture}.
 */
export class LoadGenerator extends Construct {
  /** The ECS service (ALB-fronted when exposeWebUi, else plain Fargate). The controller with `workers`. */
  public readonly service: ecs.FargateService;
  /** The service's task definition (the controller's with `workers`): its `taskRole` runs `@route` handlers. */
  public readonly taskDefinition: ecs.FargateTaskDefinition;
  /** The worker service (only with `workers > 0`); not behind the ALB. */
  public readonly workerService?: ecs.FargateService;
  /** The worker task definition (only with `workers > 0`). */
  public readonly workerTaskDefinition?: ecs.FargateTaskDefinition;
  /** CloudWatch log group the awslogs driver ships EMF stdout to. */
  public readonly logGroup: logs.LogGroup;
  public readonly logGroupName: string;
  /** Internal ALB facets — only populated when exposeWebUi. */
  public readonly loadBalancer?: elbv2.IApplicationLoadBalancer;
  public readonly loadBalancerDnsName?: string;
  public readonly loadBalancerArn?: string;
  /** The edge origin for the Locust UI — only when exposeWebUi and edgeRoute isn't false. */
  public readonly edgeOrigin?: CreEdgeOrigin;

  private readonly props: LoadGeneratorProps;
  private readonly platform: ecr_assets.Platform;
  private readonly pages = new Map<string, string>();
  private readonly deferredImage: DeferredAssetImage;
  private readonly containers: ecs.ContainerDefinition[] = [];
  private asset?: ecr_assets.DockerImageAsset;

  /**
   * The Locust image (ARM64 by default; the single source of truth for both CI providers). Built
   * from the base container, the `workloadDirectory` overlay and the pages from `addWebPage`.
   * Reading it fixes the build context, so call `addWebPage` first.
   */
  public get image(): ecr_assets.DockerImageAsset {
    if (!this.asset) {
      this.asset = new ecr_assets.DockerImageAsset(this, 'LoadGenImage', {
        directory: stageContext(this.node.path, this.props.workloadDirectory, this.pages),
        platform: this.platform,
      });
      this.deferredImage.grantPulls(this.asset);
    }
    return this.asset;
  }

  /**
   * Serve `html` on the Locust web UI at `<base path><pagePath>` (with the default edge route,
   * `/locust<pagePath>` on the edge): an operator page next to the Locust UI, behind the same
   * CloudFront Signer auth. `{{BASE_PATH}}` in the HTML becomes the base path (e.g. `/locust`), so
   * write links and `fetch()` calls as `{{BASE_PATH}}/api/...`.
   *
   * For a page that calls back into the task (a button that flushes a cache, flips a flag), put the
   * handler in the `workloadDirectory` locustfile with `@route` from `cre_pages` (POST etc.; paths
   * are relative to the base path too) and grant it AWS access with `addToRolePolicy`. Handlers
   * run in the controller (or standalone) task.
   *
   * Pages are baked into the image. Requires `exposeWebUi`.
   */
  public addWebPage(pagePath: string, html: string): void {
    if (!this.props.exposeWebUi) {
      throw new Error('LoadGenerator.addWebPage: needs exposeWebUi (pages are served by the Locust web UI).');
    }
    if (this.asset) {
      throw new Error('LoadGenerator.addWebPage: the image is already built (something read .image); add pages first.');
    }
    if (!/^\/[A-Za-z0-9._~\-/]*[A-Za-z0-9._~-]$/.test(pagePath) || pagePath.includes('//')) {
      throw new Error(`LoadGenerator.addWebPage: '${pagePath}' must start with '/', not end with '/', and use URL-safe characters (e.g. '/status').`);
    }
    if (LOCUST_ROUTES.some((r) => pagePath === r || pagePath.startsWith(`${r}/`))) {
      throw new Error(`LoadGenerator.addWebPage: '${pagePath}' is one of Locust's own routes (${LOCUST_ROUTES.join(', ')}).`);
    }
    if (this.pages.has(pagePath)) {
      throw new Error(`LoadGenerator.addWebPage: '${pagePath}' was already added.`);
    }
    this.pages.set(pagePath, html);
  }

  /** Add an environment variable to every Locust container (controller or standalone, and workers). */
  public addEnvironment(name: string, value: string): void {
    this.containers.forEach((c) => c.addEnvironment(name, value));
  }

  /** Add a statement to every Locust task role (controller or standalone, and workers). */
  public addToRolePolicy(statement: iam.PolicyStatement): void {
    this.taskDefinition.addToTaskRolePolicy(statement);
    this.workerTaskDefinition?.addToTaskRolePolicy(statement);
  }

  constructor(scope: Construct, id: string, props: LoadGeneratorProps) {
    super(scope, id);

    if (!cdk.Token.isUnresolved(props.targetUrl) && !/^https?:\/\//.test(props.targetUrl)) {
      throw new Error(
        `LoadGenerator: targetUrl '${props.targetUrl}' has no scheme — use e.g. 'https://${props.targetUrl}'.`,
      );
    }
    if (props.exposeWebUi && (props.desiredCount ?? 1) !== 1) {
      throw new Error(
        'LoadGenerator: exposeWebUi requires desiredCount 1 — each task is an independent Locust, and the ' +
          'ALB would spread UI calls (/swarm, /stop, stats) across them. Scale with `workers` instead.',
      );
    }
    const workers = props.workers ?? 0;
    if (!Number.isInteger(workers) || workers < 0) {
      throw new Error(`LoadGenerator: workers must be a whole number >= 0 (got ${workers}).`);
    }
    const distributed = workers > 0;
    if (distributed && (props.desiredCount ?? 1) !== 1) {
      throw new Error('LoadGenerator: with workers there is one controller task; leave desiredCount unset (scale with workers).');
    }
    const processes = props.processesPerTask;
    if (processes !== undefined && (!Number.isInteger(processes) || processes < 1)) {
      throw new Error(`LoadGenerator: processesPerTask must be a whole number >= 1 (got ${processes}).`);
    }
    this.props = props;

    const edgeRoute = props.exposeWebUi && props.edgeRoute !== false
      ? { name: props.edgeRoute?.name ?? 'locust', basePath: normaliseBasePath(props.edgeRoute?.basePath ?? '/locust') }
      : undefined;

    const arch = props.architecture ?? ecs.CpuArchitecture.ARM64;
    this.platform =
      arch === ecs.CpuArchitecture.ARM64
        ? ecr_assets.Platform.LINUX_ARM64
        : ecr_assets.Platform.LINUX_AMD64;

    // SINGLE SOURCE OF TRUTH. No --platform in the Dockerfile. The asset itself is
    // created at prepare time (the Aspect below), after any addWebPage() calls.
    this.deferredImage = new DeferredAssetImage(() => this.image);
    const image = this.deferredImage;
    cdk.Aspects.of(this).add({
      visit: (node: IConstruct) => {
        if (node === this) void this.image;
      },
    });

    this.logGroup = new logs.LogGroup(this, 'LoadGenLogs', {
      logGroupName: props.logGroupName ?? `/${cdk.Stack.of(this).stackName}/${id}/loadgen`,
      retention: props.logRetention ?? logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.logGroupName = this.logGroup.logGroupName;

    const environment: Record<string, string> = {
      TARGET_URL: props.targetUrl,
      TARGET_PATH: props.targetPath ?? '/',
      HTTP_METHOD: props.httpMethod ?? 'GET',
      REQUEST_TIMEOUT: String(props.requestTimeoutSeconds ?? 5),
      // C3: env var name standardized to DEMO_METRIC_NAMESPACE (shared with the
      // observability Python helper); value defaults to the shared constant.
      [DEMO_METRIC_NAMESPACE_ENV_VAR]: props.emfNamespace ?? DEMO_METRIC_NAMESPACE,
      MIN_WAIT: String(props.minWaitSeconds ?? 0.5),
      MAX_WAIT: String(props.maxWaitSeconds ?? 1.0),
      // The run size headless; the start form's defaults with the web UI.
      LOCUST_USERS: String(props.users ?? 10),
      LOCUST_SPAWN_RATE: String(props.spawnRate ?? 10),
      LOCUST_WEB_UI: props.exposeWebUi ? 'true' : 'false',
      LOCUST_ROLE: distributed ? 'controller' : 'standalone',
      ...(distributed && !props.exposeWebUi ? { LOCUST_EXPECT_WORKERS: String(workers) } : {}),
      ...(!distributed && processes ? { LOCUST_PROCESSES: String(processes) } : {}),
      // Serve the UI under the edge route's prefix: CloudFront forwards the path unchanged.
      ...(edgeRoute ? { LOCUST_WEB_BASE_PATH: edgeRoute.basePath } : {}),
      ...(props.extraTargets ?? {}),
    };

    const runtimePlatform: ecs.RuntimePlatform = {
      cpuArchitecture: arch,
      operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
    };
    const logDriver = ecs.LogDrivers.awsLogs({
      logGroup: this.logGroup,
      streamPrefix: 'loadgen',
    });
    const ulimits = props.nofileUlimit !== undefined
      ? [{ name: ecs.UlimitName.NOFILE, softLimit: props.nofileUlimit, hardLimit: props.nofileUlimit }]
      : [];

    // Workers resolve the controller through Cloud Map. A private DNS namespace is a Route 53 private
    // zone, and a VPC can't hold two with the same name, so the name is per stack and construct.
    const namespace = distributed
      ? new servicediscovery.PrivateDnsNamespace(this, 'Namespace', {
        name: `${dnsLabel(`${cdk.Stack.of(this).stackName}-${id}`)}.loadgen.internal`,
        vpc: props.vpc,
      })
      : undefined;
    const cloudMapOptions = namespace ? { name: 'controller', cloudMapNamespace: namespace } : undefined;

    if (props.exposeWebUi) {
      const loadBalancer = new elbv2.ApplicationLoadBalancer(this, 'LoadGenLB', {
        vpc: props.vpc,
        internetFacing: false,
        vpcSubnets: props.taskSubnets,
      });
      const svc = new ecs_patterns.ApplicationLoadBalancedFargateService(this, 'LoadGen', {
        vpc: props.vpc,
        cpu: props.cpu ?? 512,
        memoryLimitMiB: props.memoryLimitMiB ?? 1024,
        desiredCount: props.desiredCount ?? 1,
        minHealthyPercent: 100,
        loadBalancer,
        taskSubnets: props.taskSubnets,
        runtimePlatform,
        cloudMapOptions,
        taskImageOptions: {
          image,
          containerPort: 8089,
          environment,
          logDriver,
        },
      });
      this.taskDefinition = svc.taskDefinition;
      svc.taskDefinition.defaultContainer!.addUlimits(...ulimits);
      svc.targetGroup.configureHealthCheck({ path: edgeRoute ? `${edgeRoute.basePath}/` : '/', healthyHttpCodes: '200' });
      this.service = svc.service;
      this.loadBalancer = loadBalancer;
      this.loadBalancerDnsName = loadBalancer.loadBalancerDnsName;
      this.loadBalancerArn = loadBalancer.loadBalancerArn;
      if (edgeRoute) {
        this.edgeOrigin = new CreEdgeOrigin(this, 'EdgeOrigin', { name: edgeRoute.name, loadBalancer, vpc: props.vpc });
      }
    } else {
      // Headless: plain FargateService; the container runs `locust --headless`
      // with users/spawn-rate from env (the entrypoint honors them).
      const taskDef = new ecs.FargateTaskDefinition(this, 'LoadGenTask', {
        cpu: props.cpu ?? 512,
        memoryLimitMiB: props.memoryLimitMiB ?? 1024,
        runtimePlatform,
      });
      taskDef.addContainer('loadgen', {
        image,
        environment,
        logging: logDriver,
        ulimits,
      });
      this.taskDefinition = taskDef;
      this.service = new ecs.FargateService(this, 'LoadGen', {
        cluster: new ecs.Cluster(this, 'Cluster', { vpc: props.vpc }),
        taskDefinition: taskDef,
        desiredCount: props.desiredCount ?? 1,
        vpcSubnets: props.taskSubnets,
        cloudMapOptions,
      });
    }
    this.containers.push(this.taskDefinition.defaultContainer!);

    if (namespace) {
      // The controller listens for worker RPC on 5557; only the workers' SG may reach it.
      this.taskDefinition.defaultContainer!.addPortMappings({ containerPort: 5557 });
      const workerTaskDef = new ecs.FargateTaskDefinition(this, 'WorkerTask', {
        cpu: props.workerCpu ?? props.cpu ?? 512,
        memoryLimitMiB: props.workerMemoryLimitMiB ?? props.memoryLimitMiB ?? 1024,
        runtimePlatform,
      });
      const workerContainer = workerTaskDef.addContainer('worker', {
        image,
        environment: {
          ...environment,
          LOCUST_ROLE: 'worker',
          LOCUST_WEB_UI: 'false',
          LOCUST_CONTROLLER_HOST: `controller.${namespace.namespaceName}`,
          LOCUST_CONTROLLER_PORT: '5557',
          LOCUST_PROCESSES: String(processes ?? 1),
        },
        logging: logDriver,
        ulimits,
      });
      this.workerTaskDefinition = workerTaskDef;
      this.workerService = new ecs.FargateService(this, 'Workers', {
        cluster: this.service.cluster,
        taskDefinition: workerTaskDef,
        desiredCount: workers,
        vpcSubnets: props.taskSubnets,
      });
      this.service.connections.allowFrom(this.workerService, ec2.Port.tcp(5557), 'Locust worker RPC to controller');
      this.containers.push(workerContainer);
    }
  }
}

/** A DNS label from arbitrary text: lowercase letters, digits and hyphens, at most 63 characters. */
function dnsLabel(text: string): string {
  const label = text.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '');
  return label.slice(0, 63).replace(/-+$/, '') || 'loadgen';
}

/** `/locust/` or `locust` → `/locust`; Locust wants a leading slash and no trailing one. */
function normaliseBasePath(p: string): string {
  const trimmed = `/${p.replace(/^\/+|\/+$/g, '')}`;
  if (trimmed === '/') throw new Error('LoadGenerator: edgeRoute.basePath must not be "/" (the edge default route belongs to the app).');
  return trimmed;
}
