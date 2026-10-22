"""AWS-native EMF helper for CRE demo workloads.

Writes Embedded Metric Format JSON to stdout; CloudWatch auto-extracts metrics from the
container log group -- no PutMetricData, no agent. Metrics land in DEMO_METRIC_NAMESPACE
in the region of the log group (which MUST equal the dashboard region).

The namespace is read from the env var ``DEMO_METRIC_NAMESPACE`` (that exact env var name,
NOT ``EMF_NAMESPACE``). The CDK stack injects it onto the load-gen
/ app container; the literal default below only matters when the var is unset and MUST
match ``DEMO_METRIC_NAMESPACE`` in ``metric-namespace.ts``.

Ship as source (NOT compiled): this file is vendored alongside the workload (e.g. into the
app container).
"""
import json
import os
import time
from typing import Dict, List, Optional

#: Single source of truth for the namespace, read from the env var the CDK sets on the
#: workload. Defaults to the same literal as ``DEMO_METRIC_NAMESPACE``
#: in ``metric-namespace.ts``.
DEMO_METRIC_NAMESPACE = os.environ.get("DEMO_METRIC_NAMESPACE", "MyResilienceDemo")


def emit_emf(
    namespace: str,
    dimension_keys: List[str],
    values: Dict[str, object],
    units: Optional[Dict[str, str]] = None,
    timestamp_ms: Optional[int] = None,
) -> None:
    """Emit ONE EMF log line.

    ``values`` supplies metric name->value and dimension name->value; ``dimension_keys``
    lists which keys are dimensions (the rest are treated as metrics).
    """
    units = units or {}
    metric_names = [k for k in values if k not in dimension_keys]
    line = {
        "_aws": {
            "Timestamp": timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dimension_keys)] if dimension_keys else [[]],
                    "Metrics": [
                        {"Name": n, "Unit": units.get(n, "None")} for n in metric_names
                    ],
                }
            ],
        },
        **values,
    }
    print(json.dumps(line), flush=True)


def emit_request(
    region: str,
    success: bool,
    latency_ms: float,
    *,
    is_first: bool = True,
    namespace: str = DEMO_METRIC_NAMESPACE,
    timestamp_ms: Optional[int] = None,
) -> None:
    """Emit metrics for ONE request to ONE region endpoint.

    THE RULE: per-region metrics
    (RegionSuccess/RegionError/RegionLatency) are emitted on EVERY call (one line per
    region). Client-level metrics (ClientSuccess/ClientError/ClientLatency) are emitted on
    EXACTLY ONE line per logical request -- gate them with ``is_first`` so a request that
    fans out to N regions is counted ONCE at the client level. Passing ``is_first=True`` on
    more than one line per request double-counts; that is the single biggest footgun.

    Caller sets ``is_first=True`` for the first region line of a request and
    ``is_first=False`` for the rest. For a single-region demo, leave the default (True) and
    call once.
    """
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)

    # 1) Per-region line -- always emitted, dimensioned by Region.
    emit_emf(
        namespace=namespace,
        dimension_keys=["Region"],
        values={
            "Region": region,
            "RegionLatency": latency_ms,
            "RegionSuccess": 1 if success else 0,
            "RegionError": 0 if success else 1,
        },
        units={
            "RegionLatency": "Milliseconds",
            "RegionSuccess": "Count",
            "RegionError": "Count",
        },
        timestamp_ms=ts,
    )

    # 2) Client line -- ONLY when is_first, no dimensions (fleet aggregate).
    if is_first:
        emit_emf(
            namespace=namespace,
            dimension_keys=[],
            values={
                "ClientLatency": latency_ms,
                "ClientSuccess": 1 if success else 0,
                "ClientError": 0 if success else 1,
            },
            units={
                "ClientLatency": "Milliseconds",
                "ClientSuccess": "Count",
                "ClientError": "Count",
            },
            timestamp_ms=ts,
        )
