#!/usr/bin/env bash
# Download the DilBERT v3 memory gate model for Recall.

set -eo pipefail

if [ -n "$RECALL_HOME" ]; then
  BASE_HOME="$RECALL_HOME"
elif [ -d "$HOME/.recall" ]; then
  BASE_HOME="$HOME/.recall"
elif [ -n "$REMEMBRANCE_HOME" ]; then
  BASE_HOME="$REMEMBRANCE_HOME"
  echo "REMEMBRANCE_HOME is deprecated; using the existing directory in place." >&2
elif [ -d "$HOME/.remembrance" ]; then
  BASE_HOME="$HOME/.remembrance"
  echo "Using the existing legacy data directory in place; set RECALL_HOME to override." >&2
else
  BASE_HOME="$HOME/.recall"
fi

DEFAULT_DIR="$BASE_HOME/models/distilbert-memory-gate"
if [ "$#" -gt 0 ]; then
  TARGET_DIR="$1"
else
  TARGET_DIR="$DEFAULT_DIR"
fi

RELEASE_TAG="v3.0-dilbert-gate"
# Release assets remain at the existing remote until maintainers rename it.
REPO="emaharmony/remembrance-mcp"
BASE_URL="https://github.com/$REPO/releases/download/$RELEASE_TAG"

echo "=== DilBERT v3 Memory Gate - Model Download ==="
echo "Target: $TARGET_DIR"
mkdir -p "$TARGET_DIR"

if [ -f "$TARGET_DIR/model.safetensors" ]; then
  echo "Model already exists at $TARGET_DIR/model.safetensors"
  exit 0
fi

for FILE in config.json tokenizer.json tokenizer_config.json model.safetensors; do
  DEST="$TARGET_DIR/$FILE"
  if [ -f "$DEST" ]; then
    echo "$FILE already exists, skipping"
    continue
  fi

  echo "Downloading $FILE..."
  STATUS=$(curl -L -s -o "$DEST" -w "%{http_code}" "$BASE_URL/$FILE")
  if [ "$STATUS" != "200" ]; then
    echo "Failed to download $FILE (HTTP $STATUS)" >&2
    echo "Manual URL: $BASE_URL/$FILE" >&2
    rm -f "$DEST"
    exit 1
  fi
done

for FILE in config.json model.safetensors tokenizer.json tokenizer_config.json; do
  if [ ! -f "$TARGET_DIR/$FILE" ]; then
    echo "Missing: $FILE" >&2
    exit 1
  fi
done

echo "DilBERT v3 model installed at $TARGET_DIR"
echo "Install gate support with: pip install -e \".[gate]\""