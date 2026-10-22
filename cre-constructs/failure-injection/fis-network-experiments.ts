import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as fis from 'aws-cdk-lib/aws-fis';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

/** Fault types {@link FisNetworkExperiments} can generate. */
export type FisFault = 'latency' | 'packet-loss' | 'cpu-stress';

/**
 * FIS target selector — decouples from the source's hardcoded ASG/RDS.
 *
 * EC2 instances only: every fault here is `aws:ssm:send-command` (targets key `Instances`)
 * filtered on `Placement.AvailabilityZone`, both EC2-specific. Any other resource type
 * (e.g. `aws:ecs:task`) synthesises a template that fails at deploy or at experiment start,
 * so it is rejected at the type level and again at synth.
 */
export interface FisTarget {
  /**
   * FIS resource type. Only EC2 instances are supported.
   * @default 'aws:ec2:instance'
   */
  readonly resourceType?: 'aws:ec2:instance';
  /** Resource tag filters (e.g. `{ demo: 'my-demo' }`). */
  readonly resourceTags: Record<string, string>;
}

/**
 * Props for {@link FisNetworkExperiments}.
 *
 * SECOND, opt-in (heavier) failure-injection mechanism. L1/`Cfn*` only: `aws-cdk-lib/aws-fis`
 * has no L2.
 */
export interface FisNetworkExperimentsProps {
  /** AZs to create per-AZ experiment templates for (one set of faults per AZ). */
  readonly availabilityZones: string[];
  /**
   * Generic target selector. Filtered additionally by `Placement.AvailabilityZone`
   * per AZ. (Source hardcoded `IAutoScalingGroup` + `rds.DatabaseCluster`.)
   */
  readonly target: FisTarget;
  /**
   * Fault types to generate.
   * @default ['latency','packet-loss','cpu-stress']
   */
  readonly faults?: FisFault[];
  /**
   * Egress destinations the network faults apply to (hostnames, IPs or CIDRs, e.g. a DB
   * endpoint). Source passed the RDS cluster endpoint hostname. REQUIRED (non-empty) when
   * `faults` includes `latency` or `packet-loss`: the `AWSFIS-Run-Network-*-Sources` SSM
   * documents scope the fault to these, and with none the experiment fails or affects
   * nothing.
   * @default [] (only valid with `faults: ['cpu-stress']`)
   */
  readonly networkSources?: string[];
  /**
   * Latency injected by the latency fault.
   * @default Duration.millis(100)
   */
  readonly delay?: cdk.Duration;
  /**
   * Packet loss percent.
   * @default 10
   */
  readonly packetLossPercent?: number;
  /**
   * Per-experiment duration.
   * @default Duration.minutes(60)
   */
  readonly duration?: cdk.Duration;
  /**
   * CloudWatch alarm that auto-stops a running experiment — typically
   * `DemoObservability.availabilityAlarm`. Every template's `stopConditions` references it.
   *
   * REQUIRED. It used to be optional with a `[{ source: 'none' }]` fallback, which let a
   * forgotten prop ship a 60-minute fault with nothing to stop it; the recipe said "never
   * ship none" but nothing enforced it.
   */
  readonly stopConditionAlarm: cloudwatch.IAlarm;
  /** Log group for FIS experiment logs (created if omitted). */
  readonly logGroupName?: string;
  /** Log group retention. @default RetentionDays.ONE_WEEK */
  readonly logGroupRetention?: logs.RetentionDays;
  /** FIS log schema version. @default 2 */
  readonly logSchemaVersion?: number;
  /**
   * NIC name for network faults.
   * @default 'ens5'
   */
  readonly networkInterface?: string;
}

/**
 * SECOND, opt-in FIS construct: generates per-AZ `fis.CfnExperimentTemplate`s for
 * network latency, packet loss, and CPU stress, targeting EC2 instances by `resourceTags`
 * + a per-AZ `Placement.AvailabilityZone` filter.
 *
 *   - Tag-based EC2 target.
 *   - `stopConditionAlarm` REQUIRED: an experiment always has a way to stop.
 *   - `faults` list is opt-in (default all three).
 *   - The `addOverride` casing fix for `logGroupArn`.
 *   - A hardened FIS service role (6 AWS-managed FIS policies, a scoped CloudWatch Logs
 *     policy, SourceAccount/SourceArn assume conditions).
 *
 * **Inert at deploy**: creating a `CfnExperimentTemplate`
 * injects NOTHING — faults only occur when an experiment is *started*
 * (`fis:StartExperiment`). Never instantiated by the always-on green skeleton.
 */
export class FisNetworkExperiments extends Construct {
  public readonly latencyExperiments: fis.CfnExperimentTemplate[] = [];
  public readonly packetLossExperiments: fis.CfnExperimentTemplate[] = [];
  public readonly cpuStressExperiments: fis.CfnExperimentTemplate[] = [];
  public readonly role: iam.IRole;
  public readonly logGroup: logs.ILogGroup;

  constructor(scope: Construct, id: string, props: FisNetworkExperimentsProps) {
    super(scope, id);

    const faults = props.faults ?? ['latency', 'packet-loss', 'cpu-stress'];
    const networkInterface = props.networkInterface ?? 'ens5';
    const logSchemaVersion = props.logSchemaVersion ?? 2;
    const delay = props.delay ?? cdk.Duration.millis(100);
    const packetLossPercent = props.packetLossPercent ?? 10;
    const duration = props.duration ?? cdk.Duration.minutes(60);
    const resourceType = props.target.resourceType ?? 'aws:ec2:instance';
    // Re-checked at runtime: the literal type doesn't stop a JS caller or an `as` cast.
    if (resourceType !== 'aws:ec2:instance') {
      throw new Error(
        `FisNetworkExperiments: target.resourceType '${resourceType}' is not supported — every fault is ` +
          "aws:ssm:send-command against EC2 instances. Use 'aws:ec2:instance'.",
      );
    }
    if (!props.stopConditionAlarm) {
      throw new Error('FisNetworkExperiments: stopConditionAlarm is required (never ship stopConditions: none).');
    }
    const networkSources = props.networkSources ?? [];
    if (networkSources.length === 0 && faults.some((f) => f === 'latency' || f === 'packet-loss')) {
      throw new Error(
        'FisNetworkExperiments: networkSources must list at least one destination for the latency / ' +
          'packet-loss faults (the AWSFIS-Run-Network-*-Sources documents require Sources).',
      );
    }
    const sources = networkSources.join(',');
    // toMinutes() throws on anything that isn't whole minutes (e.g. Duration.seconds(90)).
    // Whole minutes keep the original `PT<n>M` form; anything else is expressed in seconds.
    const durationSecondsNum = duration.toSeconds();
    const durationIso =
      durationSecondsNum % 60 === 0 ? `PT${durationSecondsNum / 60}M` : `PT${durationSecondsNum}S`;
    const durationSeconds = durationSecondsNum.toString();

    this.logGroup = new logs.LogGroup(this, 'LogGroup', {
      logGroupName: props.logGroupName,
      retention: props.logGroupRetention ?? logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Scoped CloudWatch Logs managed policy.
    const cloudWatchManagedPolicy = new iam.ManagedPolicy(this, 'CwManagedPolicy', {
      description: 'Allows FIS to write CWL',
      statements: [
        new iam.PolicyStatement({
          actions: [
            'logs:CreateLogStream',
            'logs:PutLogEvents',
            'logs:DescribeLogGroups',
            'logs:DescribeLogStreams',
          ],
          effect: iam.Effect.ALLOW,
          resources: [`arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:*`],
        }),
        new iam.PolicyStatement({
          actions: [
            'logs:GetDelivery',
            'logs:GetDeliverySource',
            'logs:PutDeliveryDestination',
            'logs:GetDeliveryDestinationPolicy',
            'logs:DeleteDeliverySource',
            'logs:PutDeliveryDestinationPolicy',
            'logs:CreateDelivery',
            'logs:GetDeliveryDestination',
            'logs:PutDeliverySource',
            'logs:DeleteDeliveryDestination',
            'logs:DeleteDeliveryDestinationPolicy',
            'logs:DeleteDelivery',
          ],
          effect: iam.Effect.ALLOW,
          resources: [
            `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:delivery:*`,
            `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:delivery-source:*`,
            `arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:delivery-destination:*`,
          ],
        }),
        new iam.PolicyStatement({
          actions: [
            'logs:DescribeDeliveryDestinations',
            'logs:DescribeDeliverySources',
            'logs:DescribeDeliveries',
            'logs:CreateLogDelivery',
          ],
          effect: iam.Effect.ALLOW,
          resources: ['*'],
        }),
        new iam.PolicyStatement({
          actions: ['logs:PutResourcePolicy', 'logs:DescribeResourcePolicies', 'logs:DescribeLogGroups'],
          effect: iam.Effect.ALLOW,
          resources: [`arn:${Aws.PARTITION}:logs:${Aws.REGION}:${Aws.ACCOUNT_ID}:*`],
        }),
      ],
    });

    // Hardened FIS service role: 6 AWS-managed FIS policies + scoped CWL policy
    //.
    const role = new iam.Role(this, 'FisRole', {
      description: 'The IAM role used by FIS',
      assumedBy: new iam.ServicePrincipal('fis.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSFaultInjectionSimulatorEC2Access'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSFaultInjectionSimulatorECSAccess'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSFaultInjectionSimulatorEKSAccess'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSFaultInjectionSimulatorNetworkAccess'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSFaultInjectionSimulatorRDSAccess'),
        iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSFaultInjectionSimulatorSSMAccess'),
        cloudWatchManagedPolicy,
      ],
    });
    // SourceAccount / SourceArn assume conditions.
    const cfnRole = role.node.defaultChild as iam.CfnRole;
    cfnRole.assumeRolePolicyDocument = {
      Version: '2012-10-17',
      Statement: [
        {
          Effect: 'Allow',
          Principal: { Service: ['fis.amazonaws.com'] },
          Action: 'sts:AssumeRole',
          Condition: {
            StringEquals: { 'aws:SourceAccount': Aws.ACCOUNT_ID },
            ArnLike: {
              'aws:SourceArn': `arn:${Aws.PARTITION}:fis:${Aws.REGION}:${Aws.ACCOUNT_ID}:experiment/*`,
            },
          },
        },
      ],
    };
    this.role = role;

    const stopConditions: fis.CfnExperimentTemplate.ExperimentTemplateStopConditionProperty[] = [
      { source: 'aws:cloudwatch:alarm', value: props.stopConditionAlarm.alarmArn },
    ];

    // Loop AZs x faults, building a CfnExperimentTemplate each.
    props.availabilityZones.forEach((azName, i) => {
      const targets: Record<string, fis.CfnExperimentTemplate.ExperimentTemplateTargetProperty> = {
        oneAZ: {
          resourceType,
          selectionMode: 'ALL',
          resourceTags: props.target.resourceTags,
          filters: [{ path: 'Placement.AvailabilityZone', values: [azName] }],
        },
      };

      if (faults.includes('latency')) {
        this.latencyExperiments.push(
          this.buildExperiment(`Az${i}Latency`, {
            roleArn: role.roleArn,
            description: `Adds network latency in ${azName}`,
            actions: {
              addLatency: {
                actionId: 'aws:ssm:send-command',
                parameters: {
                  documentArn: `arn:${Aws.PARTITION}:ssm:${Aws.REGION}::document/AWSFIS-Run-Network-Latency-Sources`,
                  documentParameters: JSON.stringify({
                    Interface: networkInterface,
                    DelayMilliseconds: delay.toMilliseconds().toString(),
                    JitterMilliseconds: '10',
                    Sources: sources,
                    TrafficType: 'egress',
                    InstallDependencies: 'True',
                    DurationSeconds: durationSeconds,
                  }),
                  duration: durationIso,
                },
                targets: { Instances: 'oneAZ' },
              },
            },
            targets,
            stopConditions,
            tags: { Name: `Add Latency to ${azName}` },
            logConfiguration: {
              cloudWatchLogsConfiguration: { logGroupArn: this.logGroup.logGroupArn },
              logSchemaVersion,
            },
          }),
        );
      }

      if (faults.includes('packet-loss')) {
        this.packetLossExperiments.push(
          this.buildExperiment(`Az${i}PacketLoss`, {
            roleArn: role.roleArn,
            description: `Drops packets in ${azName}`,
            actions: {
              packetLoss: {
                actionId: 'aws:ssm:send-command',
                parameters: {
                  documentArn: `arn:${Aws.PARTITION}:ssm:${Aws.REGION}::document/AWSFIS-Run-Network-Packet-Loss-Sources`,
                  documentParameters: JSON.stringify({
                    Interface: networkInterface,
                    LossPercent: packetLossPercent.toString(),
                    Sources: sources,
                    TrafficType: 'egress',
                    InstallDependencies: 'True',
                    DurationSeconds: durationSeconds,
                  }),
                  duration: durationIso,
                },
                targets: { Instances: 'oneAZ' },
              },
            },
            targets,
            stopConditions,
            tags: { Name: `Add Packet Loss to ${azName}` },
            logConfiguration: {
              cloudWatchLogsConfiguration: { logGroupArn: this.logGroup.logGroupArn },
              logSchemaVersion,
            },
          }),
        );
      }

      if (faults.includes('cpu-stress')) {
        this.cpuStressExperiments.push(
          this.buildExperiment(`Az${i}CpuStress`, {
            roleArn: role.roleArn,
            description: `Runs CPU stress in ${azName}`,
            actions: {
              cpuStress: {
                actionId: 'aws:ssm:send-command',
                parameters: {
                  documentArn: `arn:${Aws.PARTITION}:ssm:${Aws.REGION}::document/AWSFIS-Run-CPU-Stress`,
                  documentParameters: JSON.stringify({ DurationSeconds: durationSeconds }),
                  duration: durationIso,
                },
                targets: { Instances: 'oneAZ' },
              },
            },
            targets,
            stopConditions,
            tags: { Name: `Add CPU stress to ${azName}` },
            logConfiguration: {
              cloudWatchLogsConfiguration: { logGroupArn: this.logGroup.logGroupArn },
              logSchemaVersion,
            },
          }),
        );
      }
    });
  }

  /**
   * Build one experiment template + carry forward the multi-az casing fix for
   * `logGroupArn`: the CDK L1 lower-cases the prop,
   * so re-set the PascalCase `LogGroupArn` and clear the camelCase one.
   */
  private buildExperiment(
    id: string,
    props: fis.CfnExperimentTemplateProps,
  ): fis.CfnExperimentTemplate {
    const exp = new fis.CfnExperimentTemplate(this, `${id}Template`, props);
    exp.addOverride('Properties.LogConfiguration.CloudWatchLogsConfiguration.LogGroupArn', this.logGroup.logGroupArn);
    exp.addOverride('Properties.LogConfiguration.CloudWatchLogsConfiguration.logGroupArn', undefined);
    return exp;
  }
}
