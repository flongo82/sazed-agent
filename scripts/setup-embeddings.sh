#!/usr/bin/env bash
# Prepares the search by meaning:
#   - adds the [embeddings] section (model and processors) to config.toml, when it is missing;
#   - downloads the embedding model into models/ (ignored by git: delete the folder to remove
#     it), from a fixed revision, and checks it against its known SHA-256, so that a damaged or
#     different file is never used.
# The model is Qwen3-Embedding-0.6B, quantized to 8 bits: about 639 MB, multilingual (Italian
# included), Apache 2.0; llama-server runs it on the processor. Run scripts/setup.sh first.
# Usage: scripts/setup-embeddings.sh
set -euo pipefail

MODEL="Qwen3-Embedding-0.6B-Q8_0.gguf"
SHA256="06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439"
REVISION="370f27d7550e0def9b39c1f16d3fbaa13aa67728"
SOURCE="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF/resolve/$REVISION/$MODEL"

AGENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$AGENT_DIR/config.toml"
if [ ! -f "$CONFIG" ]; then
  echo "config.toml is missing: run scripts/setup.sh first." >&2
  exit 1
fi

if ! grep -q '^\[embeddings\]' "$CONFIG"; then
  cat >> "$CONFIG" <<CONF

# Search by meaning: llama-server computes the embeddings on this computer, on the processor.
# scripts/setup-embeddings.sh downloads the model into models/.
[embeddings]
model = "models/$MODEL"
threads = 4
CONF
  echo "Added [embeddings] to config.toml."
fi

mkdir -p "$AGENT_DIR/models"
TARGET="$AGENT_DIR/models/$MODEL"
if [ -f "$TARGET" ] && echo "$SHA256  $TARGET" | sha256sum --check --status; then
  echo "The model is already there and intact: $TARGET"
  exit 0
fi

echo "Downloading $MODEL (about 639 MB)..."
curl --fail --location --silent --show-error --output "$TARGET.part" "$SOURCE"
if ! echo "$SHA256  $TARGET.part" | sha256sum --check --status; then
  rm -f "$TARGET.part"
  echo "The downloaded file does not match the expected SHA-256: nothing was saved." >&2
  exit 1
fi
mv "$TARGET.part" "$TARGET"
echo "Model saved and checked: $TARGET"
