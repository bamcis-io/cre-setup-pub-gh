import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { CreStack } from './cre-stack.js';

const VARIABLE = /^[A-Z_][A-Z0-9_]*$/;
const NAME = /^[A-Za-z][A-Za-z0-9]*$/;

/** A value another stack provides: the stack and the value, as that stack computes it. */
export interface CreStackValue {
  /** The top-level CreStack the value belongs to. */
  readonly stack: CreStack;
  /** The value in that stack (a token such as `bucket.bucketName`, or a literal). */
  readonly value: string;
  /**
   * The output name in that stack. If the stack already has an output with this name and the
   * same value, it is reused; a different value is an error. @default the parameter's id
   */
  readonly outputName?: string;
}

export interface CreCfnParameterProps extends Omit<cdk.CfnParameterProps, 'default'> {
  /**
   * Take the value from another stack. CreCfnParameter adds the output to THAT stack and this
   * parameter to this one, and the pipeline passes one to the other (never an export). This stack
   * then deploys after that one. Exactly one of `from` and `fromVariable`.
   */
  readonly from?: CreStackValue;
  /**
   * Take the value from a deploy variable (a GitLab CI/CD variable, a GitHub secret, or the local
   * environment), e.g. `'DOMAIN'`. Exactly one of `from` and `fromVariable`. The names the deploy
   * sets for every stack job (`STACK_NAME`, `AWS_REGION`, `ROLE_ARN`, `ASSETS_PREFIX`, ... and any
   * `STACK_*`) are reserved: `npx projen` refuses them.
   */
  readonly fromVariable?: string;
  /**
   * With `fromVariable`: the variable may be unset or empty (the parameter defaults to `''`).
   * @default false (the deploy stops when it is empty)
   */
  readonly optional?: boolean;
}

/**
 * A parameter the pipeline passes to a top-level stack, with its source recorded so the pipeline
 * can be generated from the app. Use it for every parameter without a default:
 *
 * ```ts
 * // another stack's value: adds the output there, the parameter here
 * const vpcId = new CreCfnParameter(this, 'VpcId', { from: { stack: network, value: network.network.vpc.vpcId }, type: 'AWS::EC2::VPC::Id' });
 * // a deploy variable
 * const domain = new CreCfnParameter(this, 'Domain', { fromVariable: 'DOMAIN' });
 * ```
 *
 * Inside a nested stack, the parameter is declared on its top-level stack (the pipeline only
 * passes top-level parameters) and CDK passes its value down into the nested stack.
 *
 * `type` defaults to `String`; give the CloudFormation type (`AWS::EC2::VPC::Id`, `Number`,
 * `CommaDelimitedList`, ...) when it isn't one.
 */
export class CreCfnParameter extends cdk.CfnParameter {
  constructor(scope: Construct, id: string, props: CreCfnParameterProps) {
    const stack = topLevelCreStack(scope, id);
    if ((props.from === undefined) === (props.fromVariable === undefined)) {
      throw new Error(`CreCfnParameter ${stack.stackName}/${id}: give exactly one of from and fromVariable.`);
    }
    if (props.optional && props.fromVariable === undefined) {
      throw new Error(`CreCfnParameter ${stack.stackName}/${id}: optional applies to fromVariable only.`);
    }
    if (!NAME.test(id)) throw new Error(`CreCfnParameter: '${id}' is not a CloudFormation parameter name.`);
    if (props.fromVariable !== undefined && !VARIABLE.test(props.fromVariable)) {
      throw new Error(`CreCfnParameter ${stack.stackName}/${id}: fromVariable '${props.fromVariable}' must be a variable name like DOMAIN (no '$').`);
    }
    // Everything is checked, and the producer's output made, before this parameter exists: a refused
    // parameter leaves nothing behind.
    const from = props.from;
    const output = from ? checkProducer(stack, id, from) : undefined;
    if (from) provideOutput(from.stack, output!, from.value);

    const { from: _from, fromVariable, optional, ...rest } = props;
    super(stack, id, { type: 'String', ...rest, ...(optional ? { default: '' } : {}) });
    this.overrideLogicalId(id);
    if (fromVariable !== undefined) {
      stack.parameterSources.set(id, { kind: 'env', variable: fromVariable, optional: optional ?? false });
    } else {
      stack.parameterSources.set(id, { kind: 'output', stack: from!.stack.stackName, output: output! });
      stack.deployAfterStack(from!.stack);
    }
  }
}

/** The output name to use, after checking the producer is another top-level CreStack. */
function checkProducer(stack: CreStack, id: string, from: CreStackValue): string {
  const producer = from.stack;
  if (!(producer instanceof CreStack) || producer.nested) {
    throw new Error(`CreCfnParameter ${stack.stackName}/${id}: from.stack must be a top-level CreStack.`);
  }
  if (producer === stack) {
    throw new Error(`CreCfnParameter ${stack.stackName}/${id}: from.stack is this stack; use the value directly.`);
  }
  const output = from.outputName ?? id;
  if (!NAME.test(output)) throw new Error(`CreCfnParameter ${stack.stackName}/${id}: '${output}' is not an output name.`);
  return output;
}

/** The CreStack a parameter belongs on: the scope's stack, or the top-level stack above a nested one. */
function topLevelCreStack(scope: Construct, id: string): CreStack {
  let stack = cdk.Stack.of(scope);
  while (stack.nestedStackParent) stack = stack.nestedStackParent;
  if (!(stack instanceof CreStack)) {
    throw new Error(`CreCfnParameter ${id}: must be inside a CreStack (from @cre/constructs).`);
  }
  return stack;
}

/** Make `producer` output `value` as `name`: reuse an output with that logical id and value, or add one. */
function provideOutput(producer: CreStack, name: string, value: string): void {
  const wanted = JSON.stringify(producer.resolve(value));
  const existing = producer.node.findAll()
    .filter((c): c is cdk.CfnOutput => c instanceof cdk.CfnOutput && cdk.Stack.of(c) === producer)
    .find((o) => producer.resolve(o.logicalId) === name);
  if (existing) {
    if (JSON.stringify(producer.resolve(existing.value)) !== wanted) {
      throw new Error(`CreCfnParameter: ${producer.stackName} already outputs ${name} with a different value; ` +
        'give from.outputName a different name.');
    }
  } else {
    new cdk.CfnOutput(producer, `CreOutput${name}`, { value }).overrideLogicalId(name);
  }
  if (!producer.consumedOutputs.includes(name)) producer.consumedOutputs.push(name);
}
