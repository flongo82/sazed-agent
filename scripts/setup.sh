#!/usr/bin/env bash
# Prepares sazed-agent on this computer:
#   - copies the agent token and the Worker's address from sazed-bot's secrets/;
#   - asks for the bot token (never shown nor passed on a command line) and checks it;
#   - writes config.toml with the same bot name and language as sazed-bot.
# Usage: scripts/setup.sh [path to sazed-bot, default: the sibling folder ../sazed-bot]
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="${1:-$AGENT_DIR/../sazed-bot}"
API_BASE="${SAZED_TELEGRAM_API_BASE:-https://api.telegram.org}" # changed only by the tests

for f in agent_token worker_url; do
  if [ ! -s "$BOT_DIR/secrets/$f" ]; then
    echo "$BOT_DIR/secrets/$f is missing: set up sazed-bot first (its setup-secrets.sh and set-webhook.sh)." >&2
    exit 1
  fi
done

mkdir -p "$AGENT_DIR/secrets" "$AGENT_DIR/data"
chmod 700 "$AGENT_DIR/secrets" "$AGENT_DIR/data"
for f in agent_token worker_url; do
  ( umask 077; tr -d '[:space:]' < "$BOT_DIR/secrets/$f" > "$AGENT_DIR/secrets/$f" )
done
echo "Copied the agent token and the Worker's address from sazed-bot."

read -rsp "Paste the bot token @BotFather gave you and press Enter (it will not be shown): " BOT_TOKEN
echo
# A paste may carry spaces, a carriage return or the terminal's bracketed-paste markers.
BOT_TOKEN="${BOT_TOKEN//$'\e[200~'/}"
BOT_TOKEN="${BOT_TOKEN//$'\e[201~'/}"
BOT_TOKEN="$(printf '%s' "$BOT_TOKEN" | tr -d '[:space:]')"
if [ -z "$BOT_TOKEN" ]; then
  echo "Nothing was pasted. In some terminals pasting needs Ctrl+Shift+V or the right mouse button." >&2
  exit 1
fi
if ! [[ "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
  echo "This does not look like a bot token: it should read like 123456789:AA..." >&2
  exit 1
fi
# The token travels on stdin, never on a command line (visible to other processes).
BOT_USERNAME="$(printf '%s' "$BOT_TOKEN" | python3 -c '
import json, sys, urllib.error, urllib.request
token, base = sys.stdin.read(), sys.argv[1]
try:
    r = json.load(urllib.request.urlopen(f"{base}/bot{token}/getMe", timeout=20))
except (urllib.error.URLError, ValueError):
    sys.exit(1)
sys.exit(1) if not r.get("ok") else print(r["result"]["username"])' "$API_BASE")" \
  || { echo "Telegram does not accept this token, or cannot be reached." >&2; exit 1; }
( umask 077; printf '%s' "$BOT_TOKEN" > "$AGENT_DIR/secrets/bot_token" )
unset BOT_TOKEN
echo "Token accepted: your bot is @$BOT_USERNAME."

CONFIG="$AGENT_DIR/config.toml"
if [ -e "$CONFIG" ]; then
  echo "config.toml already exists, and stays as it is."
else
  # Bot name and language from sazed-bot's personal configuration, when there is one.
  { read -r BOT_NAME; read -r LANGUAGE; } < <(python3 - "$BOT_DIR/worker/wrangler.jsonc" <<'EOF'
import json, re, sys
try:
    text = open(sys.argv[1], encoding="utf-8").read()
    v = json.loads(re.sub(r"^\s*//.*$", "", text, flags=re.M)).get("vars", {})
except (OSError, ValueError):
    v = {}
print((v.get("BOT_NAME") or "sazed").replace("\n", " "))
print(v.get("LANGUAGE") or "auto")
EOF
)
  python3 - "$AGENT_DIR/config.example.toml" "$CONFIG" "$BOT_NAME" "$LANGUAGE" <<'EOF'
import json, re, sys
src, dst, name, lang = sys.argv[1:]
text = open(src, encoding="utf-8").read()
# json.dumps produce una stringa TOML valida, con le virgolette e le barre già protette.
line = lambda key, value: lambda m: f"{key} = {json.dumps(value, ensure_ascii=False)}"
text, n1 = re.subn(r'^bot_name = ".*"$', line("bot_name", name), text, count=1, flags=re.M)
text, n2 = re.subn(r'^language = ".*"$', line("language", lang), text, count=1, flags=re.M)
if n1 != 1 or n2 != 1:
    sys.exit("config.example.toml has changed: cannot fill in bot_name and language")
open(dst, "w", encoding="utf-8").write(text)
EOF
  echo "Wrote config.toml: bot name \"$BOT_NAME\", language \"$LANGUAGE\"."
fi

echo
echo "Done. Next: scripts/build.sh, then scripts/test.sh."
