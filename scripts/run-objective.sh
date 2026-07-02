#!/bin/sh
# Run one objective through the brain with GUARANTEED stack teardown.
#
# Usage:  ./scripts/run-objective.sh "your objective here"
#
# Brings the compose stack up, hands the objective to the brain, and always
# tears the stack down afterwards — even on failure or Ctrl-C — so no services
# linger. Run outputs persist under data/ on the host.
set -e
cd "$(dirname "$0")/.."
DC="docker compose -f docker/docker-compose.yml"

cleanup() {
    echo "[run-objective] tearing the stack down..."
    # --profile observability so an opted-in Phoenix viewer is stopped too.
    $DC --profile observability down
}
trap cleanup EXIT INT TERM

$DC up -d
$DC exec -T brain python -m brain.main "$@"
