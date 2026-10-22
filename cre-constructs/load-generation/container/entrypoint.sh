#!/usr/bin/env sh
#
# Entry point for the generic Locust load generator.
#
# The SAME image serves every LoadGenerator topology, chosen by env the construct injects:
#
#   LOCUST_ROLE (default standalone)
#     standalone  one task generates the load itself.
#     controller      coordinator only (LoadGenerator `workers` > 0): runs no users, listens for worker
#                 RPC on 5557 and spreads the users across the connected workers.
#     worker      generator: no web UI; connects to LOCUST_CONTROLLER_HOST:LOCUST_CONTROLLER_PORT (Cloud
#                 Map name of the controller task) and forks LOCUST_PROCESSES processes, one per core.
#   LOCUST_WEB_UI=true    web UI on :8089, under LOCUST_WEB_BASE_PATH when set (the CloudFront
#                         Signer edge route, e.g. /locust; the ALB health-checks it). Otherwise
#                         headless (a headless controller waits for LOCUST_EXPECT_WORKERS workers).
#   LOCUST_USERS / LOCUST_SPAWN_RATE
#                         the run size when headless; the start form's defaults with the web UI.
#   LOCUST_PROCESSES      `--processes N` for standalone and worker.
#
# cre_pages.py (the base image's) serves LoadGenerator.addWebPage() pages and @route handlers on
# the web UI; it is loaded next to the locustfile whenever it is present.
set -eu

# --host overrides the locustfile's own (normalised) host, so strip the trailing slash here
# too, or "<api-gw-url>/" + "/path" still requests "//path".
HOST="${TARGET_URL%/}"
ROLE="${LOCUST_ROLE:-standalone}"

FILES="locustfile.py"
[ -f cre_pages.py ] && FILES="locustfile.py,cre_pages.py"
set -- -f "$FILES" --host "$HOST"

case "$ROLE" in
  worker)
    set -- "$@" --worker --master-host "${LOCUST_CONTROLLER_HOST}" --master-port "${LOCUST_CONTROLLER_PORT:-5557}"
    [ -n "${LOCUST_PROCESSES:-}" ] && set -- "$@" --processes "${LOCUST_PROCESSES}"
    exec locust "$@"
    ;;
  controller)
    set -- "$@" --master
    ;;
  standalone)
    [ -n "${LOCUST_PROCESSES:-}" ] && set -- "$@" --processes "${LOCUST_PROCESSES}"
    ;;
  *)
    echo "entrypoint: unknown LOCUST_ROLE '${ROLE}' (standalone | controller | worker)" >&2
    exit 64
    ;;
esac

set -- "$@" --users "${LOCUST_USERS:-10}" --spawn-rate "${LOCUST_SPAWN_RATE:-10}"
if [ "${LOCUST_WEB_UI:-false}" = "true" ]; then
  set -- "$@" --web-port 8089
  [ -n "${LOCUST_WEB_BASE_PATH:-}" ] && set -- "$@" --web-base-path "${LOCUST_WEB_BASE_PATH}"
else
  set -- "$@" --headless
  if [ "$ROLE" = "controller" ] && [ -n "${LOCUST_EXPECT_WORKERS:-}" ]; then
    set -- "$@" --expect-workers "${LOCUST_EXPECT_WORKERS}"
  fi
fi
exec locust "$@"
