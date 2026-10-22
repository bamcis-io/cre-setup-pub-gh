import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Aws } from 'aws-cdk-lib';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as authorizers from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';
import { CognitoAuthConstruct } from './cognito-auth-construct.js';

/**
 * Props for {@link CognitoApiGwAuthConstruct}.
 */
export interface CognitoApiGwAuthProps {
  /** Shared Cognito construct — public SPA client (`generateClientSecret: false`). */
  readonly auth: CognitoAuthConstruct;
  /** HTTP API the authorizer attaches to (business routes are added by the caller). */
  readonly httpApi: apigwv2.HttpApi;
  /** Authorizer name. Default 'CognitoJwtAuthorizer'. */
  readonly authorizerName?: string;
  /**
   * Add the public `GET /amplify-config` runtime-config route (Lambda returning
   * `{ region, userPoolId, appClientId }`). Default true. Set false to opt for the
   * deploy-time S3 `config.json` delivery alternative.
   */
  readonly addAmplifyConfigRoute?: boolean;
  /** Path for the runtime-config route. Default '/amplify-config'. */
  readonly amplifyConfigPath?: string;
}

/**
 * Variant B — API Gateway (HTTP API) Cognito JWT authorizer + `/amplify-config` runtime
 * config.
 *
 * The SPA runs the Cognito/Amplify login (public
 * client, NO hosted UI required) and sends `Authorization: Bearer <jwt>`; the authorizer
 * validates the JWT against the shared user pool.
 *
 * Caller usage for protected routes:
 * ```ts
 * httpApi.addRoutes({ path: '/api/...', methods: [...], integration, authorizer: c.authorizer });
 * ```
 *
 * ⚠️ Routes are PUBLIC unless they pass `authorizer` — an HTTP API has no API-wide auth
 * after construction (`defaultAuthorizer` only applies to routes added later, and only if
 * set when the HttpApi is created). So this construct warns at synth for every route on
 * `httpApi` with no authorizer, except the intentionally public `/amplify-config`.
 *
 * The `/amplify-config` route is created here (PUBLIC, no authorizer) so the static SPA
 * bundle discovers the Cognito IDs at runtime — decoupling the build from per-deploy /
 * per-account IDs. If the API is CloudFront-fronted, add the `Authorization`-header cache
 * policy so the bearer token is not stripped before reaching API GW.
 */
export class CognitoApiGwAuthConstruct extends Construct {
  /** Pass to `httpApi.addRoutes({ authorizer })` for protected routes. */
  public readonly authorizer: authorizers.HttpUserPoolAuthorizer;
  /** Present iff the `/amplify-config` route was created. */
  public readonly amplifyConfigFunction?: lambda.Function;

  constructor(scope: Construct, id: string, props: CognitoApiGwAuthProps) {
    super(scope, id);

    // Exactly the commented-out line at apigatewayv2-cloudfront-construct.ts:75, now real.
    this.authorizer = new authorizers.HttpUserPoolAuthorizer(
      props.authorizerName ?? 'CognitoJwtAuthorizer',
      props.auth.userPool,
      { userPoolClients: [props.auth.userPoolClient] },
    );

    if (props.addAmplifyConfigRoute ?? true) {
      this.amplifyConfigFunction = new lambda.Function(this, 'AmplifyConfigFn', {
        runtime: lambda.Runtime.PYTHON_3_12,
        handler: 'handler.handler',
        code: lambda.Code.fromAsset(path.join(__dirname, 'amplify-config', 'lambda')),
        timeout: cdk.Duration.seconds(10),
        environment: {
          REGION: Aws.REGION,
          USER_POOL_ID: props.auth.userPool.userPoolId,
          APP_CLIENT_ID: props.auth.userPoolClient.userPoolClientId,
        },
      });

      props.httpApi.addRoutes({
        path: props.amplifyConfigPath ?? '/amplify-config',
        methods: [apigwv2.HttpMethod.GET],
        integration: new integrations.HttpLambdaIntegration(
          'AmplifyConfigIntegration',
          this.amplifyConfigFunction,
        ),
        // Intentionally PUBLIC: returns non-secret, client-discoverable identifiers.
      });
    }

    // Flag routes the caller added without an authorizer. A warning, not an error: a demo
    // may want a public health route, but it should be a choice, not an omission.
    const publicByDesign = props.addAmplifyConfigRoute ?? true
      ? `GET ${props.amplifyConfigPath ?? '/amplify-config'}`
      : undefined;
    cdk.Aspects.of(props.httpApi).add({
      visit: (node) => {
        if (!(node instanceof apigwv2.CfnRoute)) return;
        const routeKey = cdk.Stack.of(node).resolve(node.routeKey);
        const authType = node.authorizationType ?? 'NONE';
        if (authType === 'NONE' && routeKey !== publicByDesign) {
          cdk.Annotations.of(node).addWarningV2(
            '@cre/auth:unauthenticatedRoute',
            `HTTP API route '${routeKey}' has no authorizer and is publicly reachable. ` +
              'Pass `authorizer: <CognitoApiGwAuthConstruct>.authorizer` to addRoutes if that is not intended.',
          );
        }
      },
    });
  }
}
