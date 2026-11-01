#!/usr/bin/env bash
# Builds the sazed-agent image with Podman (rootless). Remove it with: podman rmi sazed-agent
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
podman build --tag sazed-agent --file "$AGENT_DIR/Containerfile" "$AGENT_DIR"
