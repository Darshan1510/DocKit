#!/bin/sh
# PDF Compressor — macOS installer
#
# Downloads the latest release, installs it to ~/Downloads, strips the
# macOS quarantine attribute (so Gatekeeper doesn't block the unsigned app),
# and launches it.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/Darshan1510/pdf-compressor/main/install.sh | sh

set -eu

REPO="Darshan1510/pdf-compressor"
APP_NAME="PDFCompressor"
INSTALL_DIR="$HOME/Downloads"
APP_PATH="$INSTALL_DIR/$APP_NAME.app"

say() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
err() { printf '\033[1;31merror:\033[0m %s\n' "$1" >&2; }

# --- preflight ---

if [ "$(uname -s)" != "Darwin" ]; then
    err "This installer is for macOS only (detected: $(uname -s))."
    err "For other platforms, see https://github.com/$REPO/releases"
    exit 1
fi

ARCH=$(uname -m)
case "$ARCH" in
    arm64)
        ASSET="$APP_NAME-macOS-arm64.zip"
        ;;
    x86_64)
        err "Intel Macs are not currently supported."
        err "Only Apple Silicon (arm64) builds are published."
        err "If you need an Intel build, please open an issue at"
        err "  https://github.com/$REPO/issues"
        exit 1
        ;;
    *)
        err "Unsupported architecture: $ARCH"
        err "Expected arm64 (Apple Silicon)."
        exit 1
        ;;
esac

URL="https://github.com/$REPO/releases/latest/download/$ASSET"

# --- download ---

TMP_DIR=$(mktemp -d -t pdf-compressor-install.XXXXXX)
trap 'rm -rf "$TMP_DIR"' EXIT INT TERM

say "Installing $APP_NAME for macOS ($ARCH)"
say "Downloading $ASSET"
if ! curl -fL --progress-bar -o "$TMP_DIR/$ASSET" "$URL"; then
    err "Failed to download $URL"
    err "Check your internet connection, or visit https://github.com/$REPO/releases"
    exit 1
fi

# --- unpack ---

say "Unpacking"
if ! ditto -x -k "$TMP_DIR/$ASSET" "$TMP_DIR/unpacked"; then
    err "Failed to unpack $TMP_DIR/$ASSET"
    exit 1
fi

NEW_APP="$TMP_DIR/unpacked/$APP_NAME.app"
if [ ! -d "$NEW_APP" ]; then
    err "Expected $APP_NAME.app in the release archive but didn't find it."
    err "Archive contents:"
    ls -la "$TMP_DIR/unpacked" >&2 || true
    exit 1
fi

# --- install ---

if ! mkdir -p "$INSTALL_DIR"; then
    err "Could not create $INSTALL_DIR"
    exit 1
fi

if [ -e "$APP_PATH" ]; then
    say "Removing existing $APP_PATH"
    rm -rf "$APP_PATH"
fi

say "Moving app to $APP_PATH"
mv "$NEW_APP" "$APP_PATH"

# --- strip quarantine ---

say "Removing quarantine attribute"
xattr -cr "$APP_PATH" 2>/dev/null || true

ok "Installed to $APP_PATH"

# --- launch ---

say "Launching $APP_NAME"
open "$APP_PATH"

cat <<EOF

$APP_NAME is installed in ~/Downloads and should now be open.
You can relaunch it any time from ~/Downloads or by re-running this installer.

To update later, re-run this installer.
EOF
