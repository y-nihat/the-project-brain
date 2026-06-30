#!/bin/sh
# One-time local git setup for this repo (safe to re-run).
set -e
cd "$(dirname "$0")/.."
git config commit.template .gitmessage
git config core.hooksPath .githooks
echo "git configured:"
echo "  commit.template = $(git config commit.template)"
echo "  core.hooksPath  = $(git config core.hooksPath)"
