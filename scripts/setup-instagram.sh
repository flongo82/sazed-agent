#!/usr/bin/env bash
# Lets the agent read Instagram reels with a secondary account, beyond the public preview:
#   - takes the cookies.txt file exported from a browser where that account is logged in
#     (Netscape format, as written by extensions such as "Get cookies.txt LOCALLY");
#   - keeps only the cookies of instagram.com, checks that the session cookie is there (values
#     are never shown) and saves them in secrets/instagram_cookies.txt, readable only by you;
#   - adds their line to [secrets] in config.toml, when it is missing.
# Use a secondary account, never your own: Instagram may limit or suspend an account it sees
# used by a program. Do not log out of that browser session afterwards: logging out ends the
# session the cookies belong to. Run scripts/setup.sh first.
# Usage: scripts/setup-instagram.sh <exported cookies.txt>
set -euo pipefail

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$AGENT_DIR/config.toml"
if [ ! -f "$CONFIG" ] || [ ! -d "$AGENT_DIR/secrets" ]; then
  echo "config.toml or secrets/ is missing: run scripts/setup.sh first." >&2
  exit 1
fi
if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "Usage: scripts/setup-instagram.sh <cookies.txt exported from the browser>" >&2
  exit 1
fi

chmod 700 "$AGENT_DIR/secrets"
( umask 077; python3 - "$1" "$AGENT_DIR/secrets/instagram_cookies.txt" <<'EOF'
import sys
source, target = sys.argv[1], sys.argv[2]
kept, names = [], set()
for line in open(source, encoding="utf-8", errors="replace"):
    line = line.rstrip("\r\n")
    cookie = line[len("#HttpOnly_"):] if line.startswith("#HttpOnly_") else line
    if not cookie or cookie.startswith("#"):
        continue
    fields = cookie.split("\t")
    if len(fields) == 7 and fields[0].lstrip(".").endswith("instagram.com"):
        kept.append(line)
        if fields[6]:
            names.add(fields[5])
if not kept:
    sys.exit("No instagram.com cookies in this file: export them from instagram.com, in Netscape format.")
if "sessionid" not in names:
    sys.exit("The instagram.com cookies have no session: log in with the secondary account, then export again.")
with open(target, "w", encoding="utf-8") as out:
    out.write("# Netscape HTTP Cookie File\n" + "\n".join(kept) + "\n")
print(f"Saved {len(kept)} instagram.com cookies, session included, in secrets/instagram_cookies.txt.")
EOF
)

python3 - "$CONFIG" <<'EOF'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
if re.search(r"^instagram_cookies\s*=", text, flags=re.M):
    print("config.toml already points to them.")
    sys.exit()
m = re.search(r"^\[secrets\][^\[]*?(?=\n*^\[|\Z)", text, flags=re.M | re.S)
if not m:
    sys.exit("config.toml has no [secrets] section: add the line of config.example.toml by hand")
text = text[:m.end()] + '\ninstagram_cookies = "secrets/instagram_cookies.txt"' + text[m.end():]
open(path, "w", encoding="utf-8").write(text)
print("Added instagram_cookies to config.toml.")
EOF

echo "Now delete the exported file ($1): it holds the same session. Then restart the agent."
