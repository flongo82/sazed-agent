#!/usr/bin/env bash
# MTProto probe: with the real Telegram and the published Worker, the bot downloads through
# MTProto a file beyond the Bot API's 20 MB while the webhook keeps working. An experiment
# run by hand (spikes/mtproto.py), not a test. Keep the agent on: the probe waits for the file
# in the journal, which it reads without writing to it.
# Usage: scripts/mtproto-probe.sh [update_id of a file already in the journal]
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
for f in config.toml secrets/api_id secrets/api_hash secrets/mtproto; do
  if [ ! -e "$AGENT_DIR/$f" ]; then
    echo "$f is missing: run scripts/setup.sh, then scripts/setup-mtproto.sh." >&2
    exit 1
  fi
done
if ! podman image exists sazed-agent; then
  "$AGENT_DIR/scripts/build.sh"
fi

# Only secrets/mtproto/ is writable, for the bot's MTProto session; the rest of secrets/ is not.
podman run --rm --userns keep-id --tz local \
  --volume "$AGENT_DIR/sazed_agent:/app/sazed_agent:ro" \
  --volume "$AGENT_DIR/spikes:/app/spikes:ro" \
  --volume "$AGENT_DIR/config.toml:/app/config.toml:ro" \
  --volume "$AGENT_DIR/secrets:/app/secrets:ro" \
  --volume "$AGENT_DIR/secrets/mtproto:/app/secrets/mtproto" \
  --volume "$AGENT_DIR/data:/app/data" \
  --entrypoint python3 sazed-agent -m spikes.mtproto "$@"
