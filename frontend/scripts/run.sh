#!/usr/bin/env bash
# Bundle and launch the app. Voice needs this (not `swift run`) so the mic permission
# prompt belongs to Screen Agent rather than the terminal.
#   SCREEN_AGENT_BACKEND_URL=http://127.0.0.1:8000 scripts/run.sh
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/bundle.sh
if [[ -n "${SCREEN_AGENT_BACKEND_URL:-}" ]]; then
  open build/ScreenAgent.app --env "SCREEN_AGENT_BACKEND_URL=$SCREEN_AGENT_BACKEND_URL"
else
  open build/ScreenAgent.app
fi
