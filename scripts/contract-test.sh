#!/usr/bin/env bash
# Contract test: the real agent against the real sazed-bot Worker, both on this computer.
# It runs inside the sazed-agent image, without network access: wrangler dev runs the Worker's
# code, fake Telegram servers answer both sides, and the agent starts as in production.
# Nothing is installed: Node and wrangler come from sazed-bot (scripts/install-node.sh and
# npm install), mounted read-only; its secrets and Cloudflare login are not mounted at all.
# Usage: scripts/contract-test.sh [path to sazed-bot, default: the sibling folder ../sazed-bot]
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="$(cd "${1:-$AGENT_DIR/../sazed-bot}" && pwd)"
for p in node/bin/node worker/node_modules/wrangler/bin/wrangler.js worker/src worker/wrangler.example.jsonc; do
  if [ ! -e "$BOT_DIR/$p" ]; then
    echo "$BOT_DIR/$p is missing: set up sazed-bot first (scripts/install-node.sh, then npm install in worker/)." >&2
    exit 1
  fi
done
if ! podman image exists sazed-agent; then
  "$AGENT_DIR/scripts/build.sh"
fi

podman run --rm --network none --userns keep-id \
  --volume "$AGENT_DIR/sazed_agent:/app/sazed_agent:ro" \
  --volume "$AGENT_DIR/tests:/app/tests:ro" \
  --volume "$AGENT_DIR/config.example.toml:/app/config.example.toml:ro" \
  --volume "$BOT_DIR/node:/bot/node:ro" \
  --volume "$BOT_DIR/worker/src:/bot/worker/src:ro" \
  --volume "$BOT_DIR/worker/wrangler.example.jsonc:/bot/worker/wrangler.example.jsonc:ro" \
  --volume "$BOT_DIR/worker/node_modules:/bot/worker/node_modules:ro" \
  --entrypoint python3 sazed-agent -m tests.contract
