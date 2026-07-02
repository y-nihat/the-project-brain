#!/bin/sh
# Interactive terminal session with the brain, with guaranteed teardown.
#
# Usage:
#   ./scripts/brain-chat.sh              # brain only
#   ./scripts/brain-chat.sh --phoenix    # also start the Phoenix trace viewer
#
# Brings the stack up, drops you into `python -m brain.repl` on a TTY, and always
# tears everything down on exit (including the observability profile).
set -e
cd "$(dirname "$0")/.."
DC="docker compose -f docker/docker-compose.yml"

PROFILE=""
[ "$1" = "--phoenix" ] && PROFILE="--profile observability"

cleanup() {
    echo
    echo "[brain-chat] tearing the stack down..."
    $DC --profile observability down
}
trap cleanup EXIT INT TERM

$DC $PROFILE up -d
[ -n "$PROFILE" ] && echo "[brain-chat] Phoenix viewer at http://localhost:6006"
echo "[brain-chat] starting interactive session (Ctrl-D or /quit to exit)"
echo
# No -T: attach a TTY so input()/prompts work interactively.
$DC exec brain python -m brain.repl
