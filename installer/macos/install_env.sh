#!/bin/bash
# Sets up MOLES's private copy of Python inside MOLES.app.
# Run by the package's postinstall script:
#     install_env.sh /Applications/MOLES.app <MOLES version>
# Downloads Python (for this Mac's chip) and the pinned packages
# (installer/requirements.txt), so it needs the internet. Everything it writes
# stays inside MOLES.app, so dragging the app to the Bin removes it all;
# ~/.moles (settings, boards, logs) is never touched.
set -euo pipefail

APP="$1"
VERSION="$2"
RES="$APP/Contents/Resources"
SETUP="$RES/setup"
RUNTIME="$RES/runtime"
UV="$SETUP/uv"

# Keep uv away from any settings or Pythons already on this Mac.
export UV_NO_CONFIG=1
export UV_PYTHON_INSTALL_DIR="$RES/python"
export UV_PYTHON_PREFERENCE=only-managed
export UV_LINK_MODE=copy
export UV_CACHE_DIR="$(mktemp -d -t moles-uv-cache)"
trap 'rm -rf "$UV_CACHE_DIR"' EXIT
PYVER="$(tr -d '[:space:]' < "$SETUP/python-version.txt")"

# uv rewrites libpython after unpacking it and leaves it with a signature
# macOS rejects when Installer runs this script: Python is killed for "Code
# Signature Invalid" the moment it starts. Re-signing (ad hoc, as the files
# came) any library or program that fails macOS's own check fixes that.
resign_invalid() {
    local count=0 f
    while IFS= read -r -d '' f; do
        case "$f" in
            *.so|*.dylib) ;;
            *) /usr/bin/file -b "$f" | grep -q Mach-O || continue ;;
        esac
        if ! /usr/bin/codesign --verify "$f" 2>/dev/null; then
            /usr/bin/codesign --force --sign - "$f"
            echo "Re-signed ${f#"$1"/}"
            count=$((count + 1))
        fi
    done < <(find "$1" -type f \( -name '*.so' -o -name '*.dylib' -o -path '*/bin/*' \) -print0)
    echo "$count file(s) re-signed"
}

echo "MOLES $VERSION setup, $(date), $(uname -m)"
rm -rf "$RUNTIME" "$RES/python"

echo "== Python $PYVER"
# --no-bin: don't add python shortcuts to the user's ~/.local/bin.
"$UV" python install --no-bin "$PYVER"
# uv keeps Pythons in versioned folders next to shortcut links; the real
# folder goes to a fixed name so the app's launch script can always find it.
real="$(find "$RES/python" -mindepth 1 -maxdepth 1 -type d -name 'cpython-*' | head -n 1)"
mv "$real" "$RUNTIME"
rm -rf "$RES/python"
test -x "$RUNTIME/bin/python3"
resign_invalid "$RUNTIME"

echo "== Packages"
"$UV" pip install --python "$RUNTIME/bin/python3" --break-system-packages \
    --require-hashes --only-binary :all: --compile-bytecode \
    --requirement "$SETUP/requirements.txt"
"$UV" pip install --python "$RUNTIME/bin/python3" --break-system-packages \
    --no-deps --compile-bytecode "$SETUP"/moles-*.whl

resign_invalid "$RUNTIME"

# Lets the launcher know it was installed this way, so it offers updates.
printf '{"installer": "macos", "version": "%s"}\n' "$VERSION" > "$RUNTIME/moles-install.json"

echo "== Check"
"$RUNTIME/bin/python3" -c "import moles.ui.launcher.main_window, moles; print('MOLES', moles.__version__, 'ready')"
