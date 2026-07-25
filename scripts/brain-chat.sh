#!/bin/sh
# Interactive terminal session with the brain, with guaranteed teardown.
#
# Usage:
#   ./scripts/brain-chat.sh                       # brain only
#   ./scripts/brain-chat.sh --phoenix             # + the Phoenix trace viewer
#   ./scripts/brain-chat.sh --resume last         # continue the last conversation
#   ./scripts/brain-chat.sh --phoenix --resume <id>
#
# Brings the stack up, drops you into `python -m brain.repl` on a TTY, and always
# tears everything down on exit (including the observability profile).
set -e
cd "$(dirname "$0")/.."
DC="docker compose -f docker/docker-compose.yml --env-file .env"

# --phoenix is the LAUNCHER's flag; everything after it belongs to the REPL, so
# it is consumed here rather than forwarded (which made `--phoenix` reach
# brain.repl, print its usage and exit 2).
PROFILE=""
if [ "$1" = "--phoenix" ]; then
    PROFILE="--profile observability"
    shift
fi

cleanup() {
    echo
    echo "[brain-chat] tearing the stack down..."
    $DC --profile observability down
}
trap cleanup EXIT TERM

$DC $PROFILE up -d
[ -n "$PROFILE" ] && echo "[brain-chat] Phoenix viewer at http://localhost:6006"
echo "[brain-chat] starting interactive session (Ctrl-D or /quit to exit)"
echo
# Ignore SIGINT in the script while the REPL runs: the TTY forwards Ctrl-C to
# the REPL, which cancels the CURRENT OBJECTIVE only (teardown still happens on
# exit via the EXIT trap). No -T: attach a TTY so input()/prompts work.
trap '' INT
# "$@" is forwarded so the documented `--resume <id|prefix|last>` works through
# the launcher the README recommends; without it the flag was accepted by the
# script, dropped here, and a fresh conversation started without comment.
$DC exec brain python -m brain.repl "$@" || true
trap - INT
