#!/usr/bin/env bash
set -euo pipefail

# Install the Lightpanda headless browser binary for the fast-path engine.
# Docs: https://lightpanda.io/docs

ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"

case "$OS-$ARCH" in
    linux-x86_64|linux-amd64)
        BINARY="lightpanda-x86_64-linux"
        ;;
    linux-aarch64|linux-arm64)
        BINARY="lightpanda-aarch64-linux"
        ;;
    darwin-x86_64)
        BINARY="lightpanda-x86_64-macos"
        ;;
    darwin-arm64)
        BINARY="lightpanda-aarch64-macos"
        ;;
    *)
        echo "Unsupported platform: $OS-$ARCH"
        echo "Install manually: https://github.com/lightpanda-io/browser/releases"
        exit 1
        ;;
esac

INSTALL_DIR="${1:-/usr/local/bin}"
INSTALL_PATH="${INSTALL_DIR}/lightpanda"

echo "Installing Lightpanda ($BINARY) to $INSTALL_PATH..."

URL="https://github.com/lightpanda-io/browser/releases/download/nightly/${BINARY}"

if [ -w "$INSTALL_DIR" ]; then
    curl -fsSL -o "$INSTALL_PATH" "$URL"
    chmod +x "$INSTALL_PATH"
else
    echo "  (requires sudo for $INSTALL_DIR)"
    sudo curl -fsSL -o "$INSTALL_PATH" "$URL"
    sudo chmod +x "$INSTALL_PATH"
fi

# Verify
if "$INSTALL_PATH" version 2>/dev/null; then
    echo ""
    echo "✓ Lightpanda installed successfully: $INSTALL_PATH"
    echo "  Start: lightpanda serve --host 127.0.0.1 --port 9222"
    echo "  CDP endpoint: ws://127.0.0.1:9222"
else
    echo "✓ Binary downloaded to $INSTALL_PATH (version check may not be supported on nightly)"
fi
