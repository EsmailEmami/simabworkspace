#!/usr/bin/env bash
# =============================================================================
#  Simab Workspace — one-line install (Linux, macOS, Windows/WSL):
#
#    curl -fsSL https://raw.githubusercontent.com/EsmailEmami/simabworkspace/main/install.sh | bash
#
#  Downloads the latest release, installs the `simabworkspace` command, then
#  asks for your server (address, port, user) and sets up the SSH key.
#  A team can pre-fill its server:  ... | SIMAB_HOST=10.0.0.5 SIMAB_PORT=2222 bash
# =============================================================================
set -euo pipefail

REPO="EsmailEmami/simabworkspace"
VERSION="${SIMAB_VERSION:-latest}"
if [ "$VERSION" = "latest" ]; then
  URL="https://github.com/$REPO/releases/latest/download/simabworkspace-install.sh"
else
  URL="https://github.com/$REPO/releases/download/$VERSION/simabworkspace-install.sh"
fi

URL="${SIMAB_INSTALLER_URL:-$URL}"          # for testing a build before it is released
say() { printf '  %s\n' "$*"; }
fail() { printf '  ✗ %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) fail "Run this in Linux, macOS or WSL (on Windows use install.ps1 in PowerShell)." ;;
esac
command -v python3 >/dev/null 2>&1 || fail "python3 is required (Ubuntu/WSL: sudo apt install -y python3)."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
say "Downloading Simab Workspace ($VERSION)…"
if command -v curl >/dev/null 2>&1; then
  if [ -t 2 ]; then curl -fL --retry 3 --progress-bar "$URL" -o "$tmp/install.sh"; else curl -fsSL --retry 3 "$URL" -o "$tmp/install.sh"; fi \
    || fail "Download failed: $URL"
elif command -v wget >/dev/null 2>&1; then
  wget -q -O "$tmp/install.sh" "$URL" || fail "Download failed: $URL"
else
  fail "curl or wget is required."
fi

bash "$tmp/install.sh" --no-open

BIN="$HOME/.local/bin/simabworkspace"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/simabworkspace/config"
# First time: ask for the server, user and SSH key right here in the terminal
# (stdin is the download pipe, so read the answers from the terminal itself).
if [ -n "${SIMAB_PAIR:-}" ]; then
  # a one-time link from the web version (Computers → Connect a computer): no questions, no password
  say ""
  say "Connecting this computer to your server…"
  "$BIN" pair "$SIMAB_PAIR" && { "$BIN" gui >/dev/null 2>&1 || true; } \
    || say "The link did not work — make a new one in the web version (Computers → Connect a computer)."
elif [ ! -f "$CONFIG" ] && [ -r /dev/tty ] && [ -z "${SIMAB_NO_SETUP:-}" ]; then
  say ""
  say "Set up this machine and your server:"
  "$BIN" setup </dev/tty || say "Setup did not finish — run: simabworkspace setup"
fi

say ""
say "✓ Done. Open a new terminal (or run: export PATH=\"\$HOME/.local/bin:\$PATH\"), then:"
say "    simabworkspace          menu"
say "    simabworkspace gui      the workspace window"
