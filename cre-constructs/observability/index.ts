/**
 * Observability pattern barrel.
 *
 * Exports the always-on `DemoObservability` construct, its props, and the shared
 * `DEMO_METRIC_NAMESPACE` constant (the single source of truth consumed by the construct,
 * the load-gen container env var, and the Python EMF helper).
 *
 * The Python EMF helper (`emf/emf_helper.py`) is shipped as source and is NOT exported
 * here (it is vendored alongside the workload container, not imported by TypeScript).
 */
export * from './metric-namespace.js';
export * from './demo-observability.js';
export * from './dashboard-stack.js';
