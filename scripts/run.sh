#!/usr/bin/env bash
# Starts sazed-agent in its container. Configuration, secrets and the Whisper model are
# mounted read-only; the data folder (journal and catalogue) and the MTProto session read-write.
# Stop it with: podman stop sazed-agent
# Usage: scripts/run.sh [extra arguments for the agent, default: run]
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -f "$AGENT_DIR/config.toml" ]; then
  echo "config.toml is missing: run scripts/setup.sh first." >&2
  exit 1
fi
if ! podman image exists sazed-agent; then
  "$AGENT_DIR/scripts/build.sh"
fi
mkdir -p "$AGENT_DIR/data" "$AGENT_DIR/models"

# The bot's MTProto session is the only secret the agent writes: only that folder is mounted
# read-write, and only once scripts/setup-mtproto.sh has created it.
mtproto=()
[ -d "$AGENT_DIR/secrets/mtproto" ] && mtproto=(--volume "$AGENT_DIR/secrets/mtproto:/app/secrets/mtproto")

args=("$@")
[ ${#args[@]} -gt 0 ] || args=(run)
# Only the agent itself takes the name sazed-agent (and replaces a stopped one): another command,
# such as check, runs beside it without stopping it.
name=sazed-agent
[ "${args[0]}" = run ] || name="sazed-agent-${args[0]}"

# --tz local: i log con l'ora del computer, non quella UTC dell'immagine.
podman run --rm --name "$name" --replace --userns keep-id --tz local \
  --volume "$AGENT_DIR/config.toml:/app/config.toml:ro" \
  --volume "$AGENT_DIR/secrets:/app/secrets:ro" \
  --volume "$AGENT_DIR/data:/app/data" \
  --volume "$AGENT_DIR/models:/app/models:ro" \
  "${mtproto[@]}" \
  sazed-agent "${args[@]}"
