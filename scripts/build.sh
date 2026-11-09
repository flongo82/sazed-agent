#!/usr/bin/env bash
# Builds the sazed-agent image with Podman (rootless). Remove it with: podman rmi sazed-agent
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
podman build --tag sazed-agent --file "$AGENT_DIR/Containerfile" "$AGENT_DIR"
if command -v systemctl >/dev/null && systemctl --user is-active --quiet sazed-agent.service 2>/dev/null; then
  echo "The agent service still runs the previous image: systemctl --user restart sazed-agent"
fi
