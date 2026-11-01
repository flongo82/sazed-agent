#!/usr/bin/env bash
# Starts sazed-agent in its container. Configuration and secrets are mounted read-only, the
# data folder (journal and catalogue) read-write. Stop it with: podman stop sazed-agent
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
mkdir -p "$AGENT_DIR/data"

args=("$@")
[ ${#args[@]} -gt 0 ] || args=(run)

podman run --rm --name sazed-agent --replace --userns keep-id \
  --volume "$AGENT_DIR/config.toml:/app/config.toml:ro" \
  --volume "$AGENT_DIR/secrets:/app/secrets:ro" \
  --volume "$AGENT_DIR/data:/app/data" \
  sazed-agent "${args[@]}"
