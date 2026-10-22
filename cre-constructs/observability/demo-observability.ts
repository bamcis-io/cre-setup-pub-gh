import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import { Construct } from 'constructs';
import { DEMO_METRIC_NAMESPACE } from './metric-namespace.js'; // shared constant

export interface DemoObservabilityProps {
  /** Logical demo name; used for dashboard + alarm naming. e.g. "MyResilienceDemo". */
  readonly demoName: string;

  /**
   * EMF metric namespace the workload emits into. MUST match the namespace the
   * EMF helper writes (a shared constant, so the two can't drift). Defaults to DEMO_METRIC_NAMESPACE.
   */
  readonly metricNamespace?: string;

  /**
   * Region where EMF metrics actually land (the log group's region). Every metric and
   * every widget is stamped with this so cross-region math expressions resolve, and the
   * dashboard SHOULD be deployed into this region. Defaults to the stack's region.
   */
  readonly metricsRegion?: string;

  /** Default metric period. Defaults to 1 minute. */
  readonly period?: cdk.Duration;

  /**
   * Baseline client-availability alarm threshold (percent). Default 99.
   * Built from ClientSuccess/ClientError; sits in INSUFFICIENT_DATA until the workload
   * emits, then OK, so the stack deploys green with no workload.
   */
  readonly availabilityAlarmThreshold?: number;

  /** Create the baseline alarm. Default true. No SNS action by default (demo-safe). */
  readonly createBaselineAlarms?: boolean;

  /**
   * Create the dashboard (and its URL output). Default true. Set false in the app stack
   * when the dashboard is deployed as its own stack LAST, after edge, so it can link the
   * CloudFront operator UI: the app stack then keeps only the availability gate alarm.
   * `addMetricRow` throws when there is no dashboard.
   */
  readonly createDashboard?: boolean;

  /** Optional markdown appended under the header TextWidget (e.g. operator UI deep-links). */
  readonly headerMarkdown?: string;
}

/**
 * Always-on, hand-rolled observability baseline over GA `aws-cdk-lib/aws-cloudwatch` only.
 *
 * Creates a named dashboard with a header + a client-perceived-availability graph, ONE
 * baseline alarm (treatMissingData NOT_BREACHING, no SNS action) so an idle/empty demo
 * deploys green, and a dashboard-URL CfnOutput. Every metric and widget is stamped with
 * `metricsRegion` so dashboard region == EMF/log-group region.
 */
export class DemoObservability extends Construct {
  /** Undefined when `createDashboard` is false. */
  public readonly dashboard?: cloudwatch.Dashboard;
  public readonly availabilityAlarm?: cloudwatch.Alarm;

  private readonly namespace: string;
  private readonly metricsRegion: string;
  private readonly period: cdk.Duration;

  constructor(scope: Construct, id: string, props: DemoObservabilityProps) {
    super(scope, id);

    this.namespace = props.metricNamespace ?? DEMO_METRIC_NAMESPACE;
    // The deploy region (AWS::Region) unless the metrics live elsewhere (a dashboard in one
    // region over a client in another). CRE stacks are environment-agnostic, so the alarm and
    // the metric are both in AWS::Region.
    this.metricsRegion = props.metricsRegion ?? Aws.REGION;
    this.period = props.period ?? cdk.Duration.minutes(1);
    const threshold = props.availabilityAlarmThreshold ?? 99;

    const clientAvailability = this.availabilityExpr(
      this.metric('ClientSuccess'),
      this.metric('ClientError'),
      'Client-Perceived Availability %',
    );

    // --- Dashboard (named; on unless createDashboard: false) ----------------
    if (props.createDashboard ?? true) {
      this.dashboard = this.createDashboard(props, clientAvailability);
    }

    // --- Baseline alarm ---------------------------------------------------------
    if (props.createBaselineAlarms ?? true) {
      this.availabilityAlarm = new cloudwatch.Alarm(this, 'ClientAvailabilityAlarm', {
        alarmName: `${props.demoName}-ClientAvailability`,
        alarmDescription: `Client-perceived availability below ${threshold}% for ${props.demoName}.`,
        metric: clientAvailability,
        threshold,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 5,
        datapointsToAlarm: 3,
        // NOT_BREACHING: an idle/empty demo never false-alarms; stack is green on deploy.
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        // NO SNS action by default — demos are interactive; actions are opt-in.
      });
    }
  }

  private createDashboard(props: DemoObservabilityProps, clientAvailability: cloudwatch.IMetric): cloudwatch.Dashboard {
    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: props.demoName,
      periodOverride: cloudwatch.PeriodOverride.INHERIT,
    });

    // Row 1: header
    dashboard.addWidgets(
      new cloudwatch.TextWidget({
        width: 24,
        height: 2,
        markdown: `## ${props.demoName} — Observability\n\n${props.headerMarkdown ?? ''}`,
      }),
    );

    // Row 2: placeholder client-availability graph
    dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title: 'Client-Perceived Availability %',
        width: 24,
        height: 6,
        region: this.metricsRegion, // widget region == EMF region
        left: [clientAvailability],
        leftYAxis: { min: 0, max: 100 },
      }),
    );

    // --- Dashboard-URL output -----
    // The console's host depends on the partition (AWS::Partition), so the same template gives a
    // working link in commercial, GovCloud and China. Other partitions (EUSC, ISO) get the
    // dashboard name instead of a guessed URL.
    const isPartition = (id: string, partition: string) =>
      new cdk.CfnCondition(this, id, { expression: cdk.Fn.conditionEquals(Aws.PARTITION, partition) });
    const consoleUrl = (host: string) =>
      `https://${host}/cloudwatch/home?region=${this.metricsRegion}#dashboards:name=${props.demoName}`;
    new cdk.CfnOutput(this, 'DashboardUrl', {
      description: `CloudWatch dashboard for ${props.demoName}`,
      value: cdk.Fn.conditionIf(isPartition('PartitionIsAws', 'aws').logicalId, consoleUrl(`${this.metricsRegion}.console.aws.amazon.com`),
        cdk.Fn.conditionIf(isPartition('PartitionIsGov', 'aws-us-gov').logicalId, consoleUrl('console.amazonaws-us-gov.com'),
          cdk.Fn.conditionIf(isPartition('PartitionIsCn', 'aws-cn').logicalId, consoleUrl('console.amazonaws.cn'),
            `CloudWatch dashboard ${props.demoName} in ${this.metricsRegion}`))).toString(),
    });
    return dashboard;
  }

  /** Append a custom-metric time-series row (demo authors extend the baseline). */
  public addMetricRow(title: string, metrics: cloudwatch.IMetric[]): void {
    if (!this.dashboard) {
      throw new Error('DemoObservability.addMetricRow: no dashboard (createDashboard is false)');
    }
    this.dashboard.addWidgets(
      new cloudwatch.GraphWidget({
        title,
        width: 24,
        height: 6,
        region: this.metricsRegion,
        left: metrics,
      }),
    );
  }

  /**
   * A metric in this demo's namespace, stamped with region + period.
   * Pass `dimensions` for a per-region (or other-dimensioned) metric, omit for client/fleet.
   */
  public metric(
    name: string,
    dimensions?: Record<string, string>,
    statistic: string = 'Sum',
  ): cloudwatch.Metric {
    return new cloudwatch.Metric({
      namespace: this.namespace,
      metricName: name,
      dimensionsMap: dimensions,
      period: this.period,
      statistic,
      region: this.metricsRegion, // the load-bearing line
    });
  }

  /**
   * Availability math expression `100 * s / (s + e)`.
   */
  public availabilityExpr(
    success: cloudwatch.IMetric,
    error: cloudwatch.IMetric,
    label: string,
  ): cloudwatch.MathExpression {
    return new cloudwatch.MathExpression({
      expression: '100 * s / (s + e)',
      usingMetrics: { s: success, e: error },
      period: this.period,
      label,
    });
  }
}
