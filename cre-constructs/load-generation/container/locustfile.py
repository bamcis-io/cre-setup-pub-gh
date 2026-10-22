"""Generic single-target Locust workload for the LoadGenerator construct.

Entirely env-driven so the base image is demo-agnostic: a demo overlays its own
``locustfile.py`` (+ helper modules) via ``workloadDirectory`` without touching the
container or the cross-provider build path.

Observability (zero-infra EMF): every request prints ONE Embedded Metric Format JSON
line to stdout. The Fargate awslogs driver ships it to CloudWatch Logs, which
auto-extracts the metrics into the namespace named by ``DEMO_METRIC_NAMESPACE`` — no
MetricFilter, no extra infra. C3: the namespace env var is ``DEMO_METRIC_NAMESPACE``
(NOT ``EMF_NAMESPACE``) so this file and the observability Python helper read the same
variable the CDK stack injects.
"""

import json
import os
import time

from locust import HttpUser, between, task

# Locust joins host + path by plain concatenation, so an API Gateway URL (which ends in
# "/") plus the default path "/" requested ".../prod//". Normalise both sides.
TARGET_URL = os.environ["TARGET_URL"].rstrip("/")  # required
TARGET_PATH = "/" + os.environ.get("TARGET_PATH", "/").lstrip("/")
HTTP_METHOD = os.environ.get("HTTP_METHOD", "GET")
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "5"))
# C3: read the SAME env var name the CDK stack injects (DEMO_METRIC_NAMESPACE).
METRIC_NAMESPACE = os.environ.get("DEMO_METRIC_NAMESPACE", "MyResilienceDemo")
MIN_WAIT = float(os.environ.get("MIN_WAIT", "0.5"))
MAX_WAIT = float(os.environ.get("MAX_WAIT", "1.0"))


def client_emf(ok: bool, elapsed_ms: float) -> dict:
    """The EMF record for one request: ClientSuccess / ClientError / ClientLatency, published with
    no dimensions (the fleet: what DemoObservability's dashboard and alarm read) and by Target."""
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRIC_NAMESPACE,
                    "Dimensions": [[], ["Target"]],
                    "Metrics": [
                        {"Name": "ClientLatency", "Unit": "Milliseconds"},
                        {"Name": "ClientSuccess", "Unit": "Count"},
                        {"Name": "ClientError", "Unit": "Count"},
                    ],
                }
            ],
        },
        "Target": TARGET_PATH,
        "ClientLatency": elapsed_ms,
        "ClientSuccess": 1 if ok else 0,
        "ClientError": 0 if ok else 1,
    }


class LoadUser(HttpUser):
    host = TARGET_URL
    wait_time = between(MIN_WAIT, MAX_WAIT)

    @task
    def hit(self) -> None:
        start = time.monotonic()
        with self.client.request(
            HTTP_METHOD,
            TARGET_PATH,
            timeout=REQUEST_TIMEOUT,
            catch_response=True,
        ) as resp:
            elapsed_ms = (time.monotonic() - start) * 1000
            # 2xx/3xx only. Counting every status below 500 as success made 403s (auth),
            # 404s (wrong path) and 429s (throttling) read as green availability.
            ok = 200 <= resp.status_code < 400
            # ONE EMF line per request, so a request is counted once. It carries the
            # client contract DemoObservability reads (ClientSuccess / ClientError /
            # ClientLatency with no dimensions: its availability graph and alarm, the FIS
            # stop condition), and the same values per Target.
            print(json.dumps(client_emf(ok, elapsed_ms)), flush=True)
            if ok:
                resp.success()
            else:
                resp.failure(f"status {resp.status_code}")
