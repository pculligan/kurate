#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
API_KEY_FILE="$ROOT_DIR/conf-api-key.txt"
MERMAID_PACKAGE="@mermaid-js/mermaid-cli"

echo "Setting up Confluence Utils in $ROOT_DIR"

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -r "$ROOT_DIR/requirements.txt"

echo
echo "Checking Mermaid CLI support..."

if command -v mmdc >/dev/null 2>&1; then
  echo "Found Mermaid CLI: $(command -v mmdc)"
elif command -v npm >/dev/null 2>&1; then
  echo "Mermaid CLI not found. Attempting install with npm..."
  if npm install -g "$MERMAID_PACKAGE"; then
    if command -v mmdc >/dev/null 2>&1; then
      echo "Installed Mermaid CLI: $(command -v mmdc)"
    else
      echo "Installed $MERMAID_PACKAGE, but mmdc is still not on PATH."
      echo "Mermaid diagrams may remain as code blocks until your shell PATH is refreshed."
    fi
  else
    echo "Could not install Mermaid CLI automatically."
    echo "Mermaid diagrams will stay as code blocks unless you install $MERMAID_PACKAGE manually."
  fi
else
  echo "npm not found. Mermaid diagrams will stay as code blocks unless Mermaid CLI is installed separately."
fi

echo
echo "Setup complete."
echo

if [[ -s "$API_KEY_FILE" ]]; then
  echo "Found API key file: $API_KEY_FILE"
elif [[ -n "${CONFLUENCE_API_KEY:-}" ]]; then
  echo "Using fallback auth from CONFLUENCE_API_KEY environment variable."
else
  echo "No Confluence API token found yet."
  echo "Create $API_KEY_FILE with your token on a single line."
  echo "Fallback option: export CONFLUENCE_API_KEY=\"your-token\""
fi

echo
echo "Next steps:"
echo "source .venv/bin/activate"
echo "python3 repo_to_confluence.py --help"
