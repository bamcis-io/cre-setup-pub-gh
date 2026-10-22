import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { CreCfnParameter } from '../core/cre-cfn-parameter.js';
import { CreStack, CreStackProps } from '../core/cre-stack.js';
import { DemoObservability, DemoObservabilityProps } from './demo-observability.js';

export interface CreDashboardLink {
  readonly label: string;
  /** The edge stack output holding the URL (e.g. `EdgeUrl`); a link whose output the edge doesn't have is dropped. */
  readonly output: string;
}

/** Links every dashboard gets: the edge, and Locust when the edge has a `locust` route. */
export const DEFAULT_DASHBOARD_LINKS: CreDashboardLink[] = [
  { label: 'Locust UI (CloudFront Signer)', output: 'EdgeLocustUrl' },
  { label: 'Edge (CloudFront Signer)', output: 'EdgeUrl' },
];

export interface CreDashboardStackProps extends CreStackProps, Omit<DemoObservabilityProps, 'demoName' | 'headerMarkdown' | 'createDashboard'> {
  /** The project name (`$PROJECT_NAME`): names the dashboard and its alarm. */
  readonly projectName: string;
  /**
   * The edge stack whose URLs the header links. The dashboard then deploys after it. Without one,
   * the header has no links.
   */
  readonly edge?: CreStack & { readonly outputValues: Record<string, string> };
  /** Header links, from the edge's outputs. @default {@link DEFAULT_DASHBOARD_LINKS} */
  readonly links?: CreDashboardLink[];
  /** Extra markdown under the links. */
  readonly headerMarkdown?: string;
}

/**
 * The project's dashboard, as its own `$PROJECT_NAME-dashboard` stack. Given the `edge`, its header
 * links the edge and Locust URLs (CreCfnParameters taking the edge's outputs), so it deploys after it.
 *
 * It is a {@link DemoObservability}: a header, a client-availability graph and one alarm. That is
 * only the scaffold. Every project adds the widgets its story depends on with
 * `observability.addMetricRow(...)`, or by adding widgets to `observability.dashboard`.
 */
export class CreDashboardStack extends CreStack {
  public readonly observability: DemoObservability;

  constructor(scope: Construct, id: string, props: CreDashboardStackProps) {
    super(scope, id, props);
    const parts: string[] = [];
    const edge = props.edge;
    for (const link of edge ? props.links ?? DEFAULT_DASHBOARD_LINKS : []) {
      const value = edge!.outputValues[link.output];
      if (value === undefined) continue;
      const url = new CreCfnParameter(this, link.output, {
        from: { stack: edge!, value },
        description: `URL for "${link.label}" (output ${link.output} of ${edge!.stackName}).`,
      });
      parts.push(`- [${link.label}](${url.valueAsString})\n`);
    }
    if (props.headerMarkdown) parts.push(`\n${props.headerMarkdown}`);
    this.observability = new DemoObservability(this, 'Observability', {
      demoName: props.projectName,
      metricNamespace: props.metricNamespace,
      metricsRegion: props.metricsRegion,
      period: props.period,
      availabilityAlarmThreshold: props.availabilityAlarmThreshold,
      createBaselineAlarms: props.createBaselineAlarms,
      headerMarkdown: parts.length > 0 ? cdk.Fn.join('', parts) : undefined,
    });
  }
}
