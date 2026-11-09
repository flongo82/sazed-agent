#!/usr/bin/env bash
# Runs sazed-agent as a service of your user, with Podman Quadlet and systemd: it starts when you
# log in (or when the computer starts, with lingering: loginctl enable-linger), restarts by itself
# if it fails, and stops cleanly with the computer. The unit file, which holds this folder's path,
# goes to ~/.config/containers/systemd/ and never into the repository.
#   - same mounts as scripts/run.sh: configuration, secrets and models read-only; data/ and the
#     MTProto session (when scripts/setup-mtproto.sh has created it) read-write;
#   - after a failure it restarts in 30 seconds, except with exit code 2: another agent has taken
#     this one's place on the Worker, and fighting back would split the messages.
# Logs: journalctl --user -u sazed-agent -f. After scripts/build.sh: systemctl --user restart sazed-agent.
# Usage: scripts/install-service.sh            installs (or updates) the service and starts it
#        scripts/install-service.sh --remove   stops the service and removes it
#        scripts/install-service.sh --print    shows the unit file, and changes nothing
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/containers/systemd"
UNIT="$UNIT_DIR/sazed-agent.container"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user stop sazed-agent.service 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  echo "The sazed-agent service is stopped and removed; scripts/run.sh starts the agent by hand."
  exit 0
fi

if [ ! -f "$AGENT_DIR/config.toml" ]; then
  echo "config.toml is missing: run scripts/setup.sh first." >&2
  exit 1
fi
case "$AGENT_DIR" in
  *:*|*$'\n'*)
    echo "This folder's path contains a colon or a line break, which Podman cannot mount: move it." >&2
    exit 1 ;;
esac

# Quadlet takes paths without quotes and protects their spaces by itself.
mtproto=""
[ -d "$AGENT_DIR/secrets/mtproto" ] && mtproto="Volume=$AGENT_DIR/secrets/mtproto:/app/secrets/mtproto"
unit="# Written by sazed-agent/scripts/install-service.sh: run it again to update, --remove to remove.
[Unit]
Description=sazed-agent, the mind of sazed

[Container]
Image=localhost/sazed-agent:latest
ContainerName=sazed-agent
UserNS=keep-id
Timezone=local
Volume=$AGENT_DIR/config.toml:/app/config.toml:ro
Volume=$AGENT_DIR/secrets:/app/secrets:ro
${mtproto:+$mtproto
}Volume=$AGENT_DIR/data:/app/data
Volume=$AGENT_DIR/models:/app/models:ro
Exec=run
StopTimeout=30

[Service]
Restart=on-failure
RestartPreventExitStatus=2
RestartSec=30

[Install]
WantedBy=default.target"

if [ "${1:-}" = "--print" ]; then
  echo "$unit"
  exit 0
fi

if [ ! -x /usr/libexec/podman/quadlet ] && [ ! -x /usr/lib/podman/quadlet ]; then
  echo "Podman Quadlet is missing: it comes with Podman 4.4 or newer." >&2
  exit 1
fi
if ! podman image exists sazed-agent; then
  "$AGENT_DIR/scripts/build.sh"
fi
mkdir -p "$AGENT_DIR/data" "$AGENT_DIR/models" "$UNIT_DIR"
printf '%s\n' "$unit" > "$UNIT.tmp"
mv "$UNIT.tmp" "$UNIT"
systemctl --user daemon-reload
# The service takes the container's name: an agent started by hand with scripts/run.sh stops.
systemctl --user restart sazed-agent.service
sleep 3
if ! systemctl --user is-active --quiet sazed-agent.service; then
  echo "The service did not start: journalctl --user -u sazed-agent -n 50" >&2
  exit 1
fi
echo "sazed-agent now runs as a service of your user. Logs: journalctl --user -u sazed-agent -f"
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "It starts when you log in. To start it with the computer, before any login: loginctl enable-linger"
fi
