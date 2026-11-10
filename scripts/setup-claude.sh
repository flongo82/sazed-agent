#!/usr/bin/env bash
# Prepares the agent's access to Claude, through the API:
#   - adds the [claude] section (API address and model) and the key's line to config.toml, when
#     they are missing;
#   - asks for an API key from the Claude Console (never shown nor passed on a command line),
#     checks with the API that the key works and that it can use the configured model, and
#     saves it in secrets/.
# The key is not tied to a model: the agent names the model, from config.toml, on every request.
# Run scripts/setup.sh first. Usage: scripts/setup-claude.sh
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

echo "In the Claude Console (https://platform.claude.com), open \"API keys\" and create a key;"
echo "better one that only this agent uses, so that you can revoke it on its own."
read -rsp "Paste the key and press Enter (it will not be shown): " KEY
echo
KEY="$(clean "$KEY")"
if [[ "$KEY" == sk-ant-admin* ]]; then
  echo "This is an Admin API key, which manages the whole organization: create a regular API key." >&2
  exit 1
fi
if ! [[ "$KEY" =~ ^sk-ant-[A-Za-z0-9_-]{20,}$ ]]; then
  echo "This does not look like a Claude API key: it starts with sk-ant-." >&2
  exit 1
fi

python3 - "$CONFIG" <<'EOF'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
added = []
if not re.search(r"^anthropic_api_key\s*=", text, flags=re.M):
    # Right after the last line of [secrets], before the next section.
    m = re.search(r"^\[secrets\][^\[]*?(?=\n*^\[|\Z)", text, flags=re.M | re.S)
    if not m:
        sys.exit("config.toml has no [secrets] section: add the lines of config.example.toml by hand")
    text = text[:m.end()] + '\nanthropic_api_key = "secrets/anthropic_api_key"' + text[m.end():]
    added.append("anthropic_api_key")
if not re.search(r"^\[claude\]", text, flags=re.M):
    text = (text.rstrip("\n") + '\n\n[claude]\napi_base = "https://api.anthropic.com"\nmodel = "claude-haiku-5-5"\n'
            'careful_model = "claude-sonnet-5-5"\n')
    added.append("[claude]")
else:
    # A [claude] from before careful_model: it goes right after the line of model.
    section = re.search(r"^\[claude\][^\[]*", text, flags=re.M | re.S)
    if not re.search(r"^careful_model\s*=", section.group(0), flags=re.M):
        line = re.search(r"^model\s*=.*$", section.group(0), flags=re.M)
        at = section.start() + (line.end() if line else len(section.group(0).rstrip("\n")))
        text = text[:at] + '\ncareful_model = "claude-sonnet-5-5"' + text[at:]
        added.append("careful_model")
if added:
    open(path, "w", encoding="utf-8").write(text)
    print("Added to config.toml: " + ", ".join(added) + ".")
else:
    print("config.toml already points to them.")
EOF

# The key goes to Python on stdin, never as an argument. Exit codes: 0 the key works with the
# model, 2 the API refuses the key, 3 the key works but not the model, 4 the API is unreachable.
CHECK=$(cat <<'EOF'
import json, os, sys, tomllib, urllib.error, urllib.parse, urllib.request
claude = tomllib.load(open(sys.argv[1], "rb"))["claude"]
base = os.environ.get("SAZED_CLAUDE_API_BASE") or claude["api_base"]  # the tests point it elsewhere
key = sys.stdin.read()
uses = {"model": "to understand your messages and answer", "careful_model": "to write the cards of what you keep"}
names = []
for setting in dict.fromkeys(s for s in uses if s in claude):
    model = claude[setting]
    url = base.rstrip("/") + "/v1/models/" + urllib.parse.quote(model)
    request = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            names.append(f"{json.load(response).get('display_name') or model} ({model}) {uses[setting]}")
    except urllib.error.HTTPError as err:
        if err.code in (401, 403):
            print(f"The API refused the key ({err.code}): check that you pasted it whole and that it is active.", file=sys.stderr)
            sys.exit(2)
        if err.code == 404:
            print(f"The key works, but the API does not know the model {model}: fix [claude] {setting} in config.toml.", file=sys.stderr)
            sys.exit(3)
        print(f"The API answered {err.code}: the key could not be checked.", file=sys.stderr)
        sys.exit(4)
    except (urllib.error.URLError, TimeoutError) as err:
        print(f"The API could not be reached ({getattr(err, 'reason', err)}): the key could not be checked.", file=sys.stderr)
        sys.exit(4)
print("The API accepts the key, and the agent will use " + ", and ".join(names) + ".")
EOF
)
status=0
printf '%s' "$KEY" | python3 -c "$CHECK" "$CONFIG" || status=$?
if [ "$status" -eq 2 ]; then
  echo "Nothing saved." >&2
  exit 1
fi

chmod 700 "$AGENT_DIR/secrets"
( umask 077; printf '%s' "$KEY" > "$AGENT_DIR/secrets/anthropic_api_key" )
unset KEY
echo "Saved the key in secrets/anthropic_api_key."
if [ "$status" -eq 4 ]; then
  echo "Run this script again later to check it." >&2
fi
