/**
 * Load-generation pattern family barrel.
 *
 * OPTIONAL / off-by-default pattern — vendored on demand; NEVER instantiated by the
 * always-on green skeleton `app.ts`, and the `containerImages` project option stays false
 * in the green tree so ZERO container-build CI jobs/tasks are emitted.
 *
 * Non-TypeScript assets shipped alongside (NOT exported here — vendored as the
 * `DockerImageAsset` build context at `container/`):
 *   - `container/Dockerfile`        — generic base image (NO `--platform`; from the asset).
 *   - `container/requirements.txt`  — locust + requests, pinned.
 *   - `container/locustfile.py`     — generic env-driven single-target workload, one EMF
 *                                     line per request → stdout → awslogs → CloudWatch.
 *   - `container/entrypoint.sh`     — web-UI vs headless switch via env.
 */
export * from './load-generator.js';
