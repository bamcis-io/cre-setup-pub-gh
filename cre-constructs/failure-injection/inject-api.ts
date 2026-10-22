import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as apigateway from 'aws-cdk-lib/aws-apigateway';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { FailureInjectionParameter } from './failure-injection-parameter.js';

/**
 * Props for {@link InjectApi}.
 *
 * OPTIONAL sub-construct: a POST/GET `/inject` API +
 * Lambda for demos that want a button/API instead of the CLI scripts.
 */
export interface InjectApiProps {
  /** The knob whose params this API controls. */
  readonly knob: FailureInjectionParameter;
  /**
   * Logical target -> SSM param name. Defaults to a single `"sync"` target mapped to the
   * primary param plus one entry per additional knob.
   */
  readonly targetMap?: Record<string, string>;
  /** Existing RestApi to attach POST/GET `/inject` to (else one is created). */
  readonly restApi?: apigateway.IRestApi;
  /**
   * Serve `/inject` without authentication. Off by default: the API is public, and
   * anyone holding the URL could otherwise set every knob to 100% failure. With the
   * default (`AWS_IAM`), callers sign requests with SigV4 — `grantInvoke` a role, or call
   * it with `curl --aws-sigv4`. Set this only for a short-lived demo you accept anyone can
   * break; synth emits a warning when you do.
   * @default false
   */
  readonly allowUnauthenticated?: boolean;
}

/**
 * OPTIONAL control-plane API for the error-rate knob. Adds an inject Lambda (its
 * `ssm:PutParameter`/`ssm:GetParameter` write scope is exactly the knob's param ARNs via
 * `knob.grantWrite`) behind POST/GET `/inject`.
 *
 * Not instantiated by the green skeleton; vendored only when a demo wants a button/UI.
 */
export class InjectApi extends Construct {
  public readonly injectFn: lambda.Function;
  public readonly api: apigateway.IRestApi;
  private readonly methods: apigateway.Method[];

  constructor(scope: Construct, id: string, props: InjectApiProps) {
    super(scope, id);

    // Default map: the primary param + one entry per additional knob (by its param name).
    const targetMap: Record<string, string> = props.targetMap ?? {
      sync: props.knob.parameterName,
      ...props.knob.additionalNames,
    };

    this.injectFn = new lambda.Function(this, 'InjectFn', {
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, 'inject-api', 'lambda', 'inject')),
      timeout: cdk.Duration.seconds(5),
      environment: { TARGET_MAP: JSON.stringify(targetMap) },
    });

    // Scope writes to exactly the knob's param ARNs.
    props.knob.grantWrite(this.injectFn);
    // GET /inject reads current rates (handler calls get_parameter), so the Lambda
    // also needs scoped read — without this, GET silently AccessDenies and reports 0.
    props.knob.grantRead(this.injectFn);

    this.api =
      props.restApi ??
      new apigateway.RestApi(this, 'InjectApiGw', {
        restApiName: `${id}-inject`,
      });

    const authorizationType = props.allowUnauthenticated
      ? apigateway.AuthorizationType.NONE
      : apigateway.AuthorizationType.IAM;
    if (props.allowUnauthenticated) {
      cdk.Annotations.of(this).addWarningV2(
        '@cre-demo-template/failure-injection:injectApiUnauthenticated',
        '/inject is public and unauthenticated: anyone with the URL can set the failure knobs.',
      );
    }

    const r = this.api.root.addResource('inject'); // POST/GET /inject (:191-193)
    this.methods = ['POST', 'GET'].map((m) =>
      r.addMethod(m, new apigateway.LambdaIntegration(this.injectFn), { authorizationType }),
    );
  }

  /** Allow `grantee` to call POST/GET `/inject` (needed under the default IAM auth). */
  public grantInvoke(grantee: iam.IGrantable): iam.Grant {
    return iam.Grant.addToPrincipal({
      grantee,
      actions: ['execute-api:Invoke'],
      resourceArns: this.methods.map((m) => m.methodArn),
    });
  }
}
