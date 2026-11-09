#!/usr/bin/env bash
# Start the show runner with the Venice key from 1Password (approve the 1Password prompt).
#   ./showrunner/run.sh                 # simulated chat
#   ./showrunner/run.sh --chat kick          # real Kick chat (channel fielding)
set -euo pipefail
cd "$(dirname "$0")/.."
# Keys come from the environment if set, else from 1Password (`op`; approve its prompt).
VENICE_API_KEY="${VENICE_API_KEY:-$(op read 'op://Private/VENICE_API_KEY/credential')}"
export VENICE_API_KEY
# Kick bot app credentials (optional: without them the bot is simply off)
KICK_CLIENT_ID="${KICK_CLIENT_ID:-$(op read 'op://Private/HUBIE_KICK_BOT/client_id' 2>/dev/null || true)}"
KICK_CLIENT_SECRET="${KICK_CLIENT_SECRET:-$(op read 'op://Private/HUBIE_KICK_BOT/client_secret' 2>/dev/null || true)}"
export KICK_CLIENT_ID KICK_CLIENT_SECRET
# OBS websocket password for auto-clips (optional: without it clips are off)
OBS_WS_PASSWORD="${OBS_WS_PASSWORD:-$(op read 'op://Private/HUBIE_OBS_WS/password' 2>/dev/null || true)}"
export OBS_WS_PASSWORD
[ $# -eq 0 ] && set -- --chat sim --sim-every 3 --vote-seconds 60
# Restart on crash without another 1Password prompt (the key stays in this process's env
# only), so an unattended stream recovers on its own. Ctrl-C stops it for good.
trap 'exit 0' INT TERM
while true; do
  uv run -q --with websockets --with httpx --with requests --with pyobjc-framework-Quartz --with pillow python -m showrunner.main "$@" || true
  echo "show runner exited; restarting in 3s" >&2
  sleep 3
done
