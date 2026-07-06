#!/bin/sh
# Run the behavior eval suite with GUARANTEED stack teardown.
#
# Usage:  ./scripts/run-evals.sh [scenario-id ...]     (default: all scenarios)
#
# Costs real tokens and minutes — run after major engine changes, not per
# commit. Scorecards persist under data/evals/ on the host; exit code is 0
# only when every check in every scenario passed.
set -e
cd "$(dirname "$0")/.."
DC="docker compose -f docker/docker-compose.yml"

cleanup() {
    echo "[run-evals] tearing the stack down..."
    $DC --profile observability down
}
trap cleanup EXIT INT TERM

$DC up -d
$DC exec -T brain python -m brain.evals run "$@"
