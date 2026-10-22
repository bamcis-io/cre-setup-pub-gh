/**
 * Failure-injection pattern family barrel.
 *
 * OPTIONAL pattern — vendored on demand; NEVER instantiated by the always-on green
 * skeleton `app.ts`. Both variants are inert at deploy:
 *   - {@link FailureInjectionParameter} (DEFAULT, lightweight SSM knob) seeds to "0".
 *   - {@link FisNetworkExperiments} (opt-in, heavier) creates experiment TEMPLATES only.
 *
 * Non-TypeScript assets shipped alongside (NOT exported here — vendored as source):
 *   - `app-helper/error_rate.py`     — app-side read-with-TTL knob reader.
 *   - `scripts/inject-failure.sh`, `scripts/restore.sh` — param-name-parameterized.
 *   - `inject-api/lambda/inject/handler.py` — the {@link InjectApi} Lambda (Code.fromAsset).
 */
export * from './failure-injection-parameter.js';
export * from './inject-api.js';
export * from './fis-network-experiments.js';
