#!/usr/bin/env bash
# Prepares the MTProto access the agent uses to download files beyond the Bot API's 20 MB:
#   - asks for api_id and api_hash (from my.telegram.org, never shown nor passed on a command
#     line) and saves them in secrets/;
#   - creates secrets/mtproto/, where the agent keeps its MTProto session;
#   - adds the matching lines to config.toml, when they are missing.
# Run scripts/setup.sh first. Usage: scripts/setup-mtproto.sh
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$AGENT_DIR/config.toml"
if [ ! -f "$CONFIG" ] || [ ! -d "$AGENT_DIR/secrets" ]; then
  echo "config.toml or secrets/ is missing: run scripts/setup.sh first." >&2
  exit 1
fi

# A paste may carry spaces, a carriage return or the terminal's bracketed-paste markers.
clean() {
  local value="${1//$'\e[200~'/}"
  value="${value//$'\e[201~'/}"
  printf '%s' "$value" | tr -d '[:space:]'
}

echo "On https://my.telegram.org open \"API development tools\" and create an application."
read -rsp "Paste its api_id and press Enter (it will not be shown): " API_ID
echo
API_ID="$(clean "$API_ID")"
if ! [[ "$API_ID" =~ ^[0-9]{1,12}$ ]]; then
  echo "This does not look like an api_id: it is a number, like 1234567." >&2
  exit 1
fi
read -rsp "Paste its api_hash and press Enter (it will not be shown): " API_HASH
echo
API_HASH="$(clean "$API_HASH")"
if ! [[ "$API_HASH" =~ ^[0-9a-fA-F]{32}$ ]]; then
  echo "This does not look like an api_hash: it has 32 letters and digits, from 0-9 and a-f." >&2
  exit 1
fi

mkdir -p "$AGENT_DIR/secrets/mtproto"
chmod 700 "$AGENT_DIR/secrets" "$AGENT_DIR/secrets/mtproto"
( umask 077
  printf '%s' "$API_ID" > "$AGENT_DIR/secrets/api_id"
  printf '%s' "$API_HASH" > "$AGENT_DIR/secrets/api_hash" )
unset API_ID API_HASH
echo "Saved api_id and api_hash in secrets/."

python3 - "$CONFIG" <<'EOF'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
lines = {"api_id": "secrets/api_id", "api_hash": "secrets/api_hash", "mtproto_dir": "secrets/mtproto"}
missing = [f'{key} = "{value}"' for key, value in lines.items()
           if not re.search(rf"^{key}\s*=", text, flags=re.M)]
if not missing:
    print("config.toml already points to them.")
    sys.exit()
# Right after the last line of [secrets], before the next section.
m = re.search(r"^\[secrets\][^\[]*?(?=\n*^\[|\Z)", text, flags=re.M | re.S)
if not m:
    sys.exit("config.toml has no [secrets] section: add the lines of config.example.toml by hand")
text = text[:m.end()] + "\n" + "\n".join(missing) + text[m.end():]
open(path, "w", encoding="utf-8").write(text)
print("Added to config.toml: " + ", ".join(line.split()[0] for line in missing) + ".")
EOF

echo
echo "Done. The agent logs in with the bot token: the session is the bot's, not your account's."
