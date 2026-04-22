#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
IDENTITY_FILE="$ROOT_DIR/confluence-identity.yaml"
MERMAID_PACKAGE="@mermaid-js/mermaid-cli"

echo "Setting up the Knowledge Cleanup Suite in $ROOT_DIR"

needs_venv_rebuild() {
  if [[ ! -d "$VENV_DIR" ]]; then
    return 0
  fi

  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    return 0
  fi

  if [[ -f "$VENV_DIR/bin/pip" ]]; then
    local pip_shebang
    pip_shebang="$(head -n 1 "$VENV_DIR/bin/pip" || true)"
    if [[ "$pip_shebang" == "#!"*"/.venv/bin/python"* ]] && [[ "$pip_shebang" != "#!$VENV_DIR/bin/python"* ]]; then
      return 0
    fi
  fi

  return 1
}

if needs_venv_rebuild; then
  echo "Rebuilding virtualenv at $VENV_DIR to repair stale interpreter paths..."
  rm -rf "$VENV_DIR"
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"

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

if [[ -f "$IDENTITY_FILE" ]] && grep -Eq '^[[:space:]]*api_key:[[:space:]]*[^[:space:]]' "$IDENTITY_FILE"; then
  echo "Found API key in identity config: $IDENTITY_FILE"
else
  echo "No Confluence API token found yet."
  echo "Add api_key: your-token to $IDENTITY_FILE."
fi

if [[ -f "$IDENTITY_FILE" ]]; then
  echo "Found identity config: $IDENTITY_FILE"
else
  echo "Identity config not found yet."
  echo "Create $IDENTITY_FILE with your base URL, email, and api_key."
fi

echo
echo "Next steps:"
echo "source .venv/bin/activate  # bash/zsh"
echo "python3 kurate.py --help"
