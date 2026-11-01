#!/usr/bin/env bash
# Runs the tests inside the sazed-agent image, against the code of this folder (mounted
# read-only, so no rebuild is needed after each change) and without network access.
# Usage: scripts/test.sh [unittest arguments, e.g. tests.test_journal]
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if ! podman image exists sazed-agent; then
  "$AGENT_DIR/scripts/build.sh"
fi

args=("$@")
[ ${#args[@]} -gt 0 ] || args=(discover --start-directory tests --top-level-directory . --verbose)

podman run --rm --network none --userns keep-id \
  --volume "$AGENT_DIR/sazed_agent:/app/sazed_agent:ro" \
  --volume "$AGENT_DIR/tests:/app/tests:ro" \
  --volume "$AGENT_DIR/scripts:/app/scripts:ro" \
  --volume "$AGENT_DIR/config.example.toml:/app/config.example.toml:ro" \
  --entrypoint python3 sazed-agent -m unittest "${args[@]}"
