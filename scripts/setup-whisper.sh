#!/usr/bin/env bash
# Prepares the transcription of voice messages, audio and videos:
#   - adds the [whisper] section (model and processors) to config.toml, when it is missing;
#   - downloads the whisper.cpp model into models/ (ignored by git: delete the folder to remove
#     it) and checks it against its known SHA-256, so that a damaged or different file is never
#     used.
# The model is large-v3-turbo, quantized to 5 bits: about 574 MB, good with Italian, a little
# slower than real time on the processor. Run scripts/setup.sh first.
# Usage: scripts/setup-whisper.sh
set -euo pipefail

MODEL="ggml-large-v3-turbo-q5_0.bin"
SHA256="394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2"
SOURCE="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$MODEL"

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$AGENT_DIR/config.toml"
if [ ! -f "$CONFIG" ]; then
  echo "config.toml is missing: run scripts/setup.sh first." >&2
  exit 1
fi

if ! grep -q '^\[whisper\]' "$CONFIG"; then
  cat >> "$CONFIG" <<EOF

# Voice messages, audio and videos are transcribed on this computer, by whisper.cpp on the
# processor, at low priority. scripts/setup-whisper.sh downloads the model into models/.
[whisper]
model = "models/$MODEL"
threads = 8
EOF
  echo "Added [whisper] to config.toml."
fi

mkdir -p "$AGENT_DIR/models"
TARGET="$AGENT_DIR/models/$MODEL"
if [ -f "$TARGET" ] && echo "$SHA256  $TARGET" | sha256sum --check --status; then
  echo "The model is already there and intact: $TARGET"
  exit 0
fi

echo "Downloading $MODEL (about 574 MB)..."
curl --fail --location --silent --show-error --output "$TARGET.part" "$SOURCE"
if ! echo "$SHA256  $TARGET.part" | sha256sum --check --status; then
  rm -f "$TARGET.part"
  echo "The downloaded file does not match the expected SHA-256: nothing was saved." >&2
  exit 1
fi
mv "$TARGET.part" "$TARGET"
echo "Model saved and checked: $TARGET"
