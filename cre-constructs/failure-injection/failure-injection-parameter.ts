import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

/**
 * Props for {@link FailureInjectionParameter}.
 *
 * DEFAULT failure-injection mechanism: a lightweight, app-cooperative SSM "error-rate knob"
 * whose parameter name derives from `demoName`.
 */
export interface FailureInjectionParameterProps {
  /** Logical demo name; used to build the default parameter name. */
  readonly demoName: string;
  /**
   * Full SSM parameter name for the primary knob.
   * @default `/${demoName}/error-rate`
   */
  readonly parameterName?: string;
  /**
   * Initial value. MUST seed "0" to keep the skeleton GREEN at deploy (a "0" error rate
   * is a no-op, and the knob is also inert until an app reads + acts on it).
   * @default '0'
   */
  readonly initialValue?: string;
  /**
   * Extra named knobs created alongside the primary one. Key = logical name;
   * value = initial value (use "0").
   *
   * e.g. `{ 'async-ingest': '0', 'queue': '0' }`
   *   -> `/${demoName}/async-ingest-error-rate`, `/${demoName}/queue-error-rate`
   * @default {}
   */
  readonly additionalKnobs?: Record<string, string>;
}

/**
 * DEFAULT (lightweight) failure-injection construct: a console/CLI-rotatable SSM
 * `StringParameter` "error-rate knob" seeded to `"0"`.
 *
 * **Inert at deploy**: the parameter seeds to `"0"` (a
 * no-op) and changes nothing until the demo's app reads `ERROR_RATE_PARAM` and acts on it.
 * Never instantiated by the always-on green skeleton `app.ts` — vendored on demand.
 */
export class FailureInjectionParameter extends Construct {
  /** The primary error-rate parameter. */
  public readonly parameter: ssm.StringParameter;
  /** Resolved name of the primary parameter (use for env var + CfnOutput). */
  public readonly parameterName: string;
  /** Additional named knobs, keyed by the logical name passed in props. */
  public readonly additional: Record<string, ssm.StringParameter>;
  /**
   * Resolved (literal) param names of the additional knobs, keyed by logical name.
   * Use these for the {@link InjectApi} target map — `ssm.StringParameter.parameterName`
   * resolves to a CFN token, not the literal string.
   */
  public readonly additionalNames: Record<string, string>;

  constructor(scope: Construct, id: string, props: FailureInjectionParameterProps) {
    super(scope, id);

    this.parameterName = props.parameterName ?? `/${props.demoName}/error-rate`;
    this.parameter = new ssm.StringParameter(this, 'ErrorRateParam', {
      parameterName: this.parameterName,
      stringValue: props.initialValue ?? '0', // seeds GREEN
      description: 'Error rate percentage (0-100) for failure injection',
    });

    this.additional = {};
    this.additionalNames = {};
    for (const [name, initial] of Object.entries(props.additionalKnobs ?? {})) {
      const knobName = `/${props.demoName}/${name}-error-rate`;
      this.additionalNames[name] = knobName;
      this.additional[name] = new ssm.StringParameter(this, `Knob-${name}`, {
        parameterName: knobName,
        stringValue: initial ?? '0',
        description: `Error rate percentage (0-100) for ${name}`,
      });
    }

    // Convention: emit the primary param name so the inject/restore scripts are
    // demo-independent (they read this CfnOutput, never a hardcoded name).
    new cdk.CfnOutput(this, 'ErrorRateParamName', {
      value: this.parameterName,
      description: 'SSM parameter name for the inject/restore scripts',
    });
  }

  /** Grant a consumer (Lambda/role) read access to ALL knobs. */
  public grantRead(grantee: iam.IGrantable): iam.Grant {
    const g = this.parameter.grantRead(grantee);
    for (const p of Object.values(this.additional)) {
      p.grantRead(grantee);
    }
    return g;
  }

  /** Grant a control-plane principal write access (for an {@link InjectApi}). */
  public grantWrite(grantee: iam.IGrantable): iam.Grant {
    const g = this.parameter.grantWrite(grantee);
    for (const p of Object.values(this.additional)) {
      p.grantWrite(grantee);
    }
    return g;
  }

  /** ARNs of all knobs (for scoping a custom inject Lambda policy). */
  public get parameterArns(): string[] {
    return [
      this.parameter.parameterArn,
      ...Object.values(this.additional).map((p) => p.parameterArn),
    ];
  }
}
