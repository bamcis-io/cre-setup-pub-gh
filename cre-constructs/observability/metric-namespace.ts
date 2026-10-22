/**
 * The single source of truth for the EMF metric
 * namespace shared across the observability construct, the Python EMF helper, and the
 * load-gen container env var.
 *
 * MUST match the value the workload's EMF helper writes. Consumed as the default for
 * {@link DemoObservabilityProps.metricNamespace}. The Python helper reads the same value
 * from the `DEMO_METRIC_NAMESPACE` env var (C3 — that exact env var name), which the CDK
 * stack injects onto the load-gen container so the running value is authoritative and the
 * literals only matter as defaults.
 *
 * One constant, so the dashboard and the emitter can't drift onto different namespaces.
 */
export const DEMO_METRIC_NAMESPACE = 'MyResilienceDemo';

/**
 * The env var name the CDK stack injects onto the load-gen / app container and the Python
 * EMF helper reads. Standardized as `DEMO_METRIC_NAMESPACE`
 * (NOT `EMF_NAMESPACE`) — observability owns the namespace concept.
 */
export const DEMO_METRIC_NAMESPACE_ENV_VAR = 'DEMO_METRIC_NAMESPACE';
