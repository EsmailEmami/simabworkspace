#!/usr/bin/env bash
# =============================================================================
#  Build what you give to people:
#    dist/simabworkspace-install.sh        one file — run it to install or update
#    dist/simabworkspace-<version>.tar.gz  the same files as an archive
#    dist/SHA256SUMS
#  Usage:  bash release.sh
# =============================================================================
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
version=$(sed -n 's/^VERSION="\(.*\)"$/\1/p' "$here/simabworkspace.sh" | head -n 1)
[ -n "$version" ] || { echo "VERSION not found in simabworkspace.sh" >&2; exit 1; }
dist="${DIST:-$here/dist}"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

# --- what goes in: the script, the window and the guide (no tests, no caches)
mkdir -p "$stage/simabworkspace"
cp "$here/simabworkspace.sh" "$here/README.md" "$stage/simabworkspace/"
cp -R "$here/gui" "$stage/simabworkspace/gui"
find "$stage/simabworkspace" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) -prune -exec rm -rf {} +
chmod +x "$stage/simabworkspace/simabworkspace.sh"

mkdir -p "$dist"
tarball="$dist/simabworkspace-$version.tar.gz"
tar -C "$stage" -czf "$tarball" simabworkspace

# --- one-file installer: a small script with the archive appended (base64)
installer="$dist/simabworkspace-install.sh"
{
  cat <<EOF
#!/usr/bin/env bash
# Simab Workspace $version — installer / updater
#   bash simabworkspace-install.sh             install (or update) and open the window
#   bash simabworkspace-install.sh --no-open   install only
# Works on Linux, WSL, macOS and Windows (Git Bash). No sudo needed for the app itself.
set -euo pipefail
VERSION="$version"
EOF
  cat <<'EOF'
say() { printf '  %s\n' "$*"; }
for c in bash tar base64; do
  command -v "$c" >/dev/null 2>&1 || { say "✗ '$c' is required. Install it first (e.g. sudo apt install $c)."; exit 1; }
done
# python3 — or on Windows python / py (`python3` there can be the Store's placeholder)
py_ok=0
for c in python3 python py; do
  command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1 && { py_ok=1; break; }
done
[ $py_ok -eq 1 ] || { say "✗ Python 3 is required (Linux: sudo apt install python3; Windows: winget install Python.Python.3.12)."; exit 1; }
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/simabworkspace/app"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# everything after the __ARCHIVE__ line is the app
sed -n '/^__ARCHIVE__$/,$p' "$0" | tail -n +2 | base64 -d 2>/dev/null | tar -xz -C "$tmp" \
  || sed -n '/^__ARCHIVE__$/,$p' "$0" | tail -n +2 | base64 -D | tar -xz -C "$tmp"
mkdir -p "$(dirname "$DEST")"
rm -rf "$DEST.new"
mv "$tmp/simabworkspace" "$DEST.new"
rm -rf "$DEST"
mv "$DEST.new" "$DEST"
say "✓ Simab Workspace $VERSION unpacked to $DEST"
bash "$DEST/simabworkspace.sh" self-install
say "✓ Command installed: simabworkspace   (open a new terminal if it is not found yet)"
if [ "${1:-}" = "--no-open" ]; then
  say "Open the window with:  simabworkspace gui"
  exit 0
fi
say "Opening the window… (first time: it shows Settings to set up this machine and your server)"
bash "$DEST/simabworkspace.sh" gui || say "Could not open the window — run: simabworkspace gui"
exit 0
__ARCHIVE__
EOF
  base64 "$tarball"
} > "$installer"
chmod +x "$installer"

(cd "$dist" && sha256sum "$(basename "$installer")" "$(basename "$tarball")" > SHA256SUMS)
echo "Simab Workspace $version"
ls -lh "$installer" "$tarball" | awk '{print "  " $5 "  " $9}'
