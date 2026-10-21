#!/bin/bash
# Regenerate installer/requirements.txt: the exact, hash-checked package list
# every installer downloads, for macOS (Intel + Apple Silicon) and Windows.
# Run from anywhere after changing pyproject.toml dependencies or
# installer/constraints.txt; needs uv (https://docs.astral.sh/uv/).
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_VERSION="$(cat installer/python-version.txt)"

UV_NO_CONFIG=1 uv pip compile pyproject.toml \
    --universal --python-version "$PYTHON_VERSION" \
    --only-binary :all: --generate-hashes --no-header \
    --constraint installer/constraints.txt \
    --output-file installer/requirements.txt

# Prove every pinned package has a ready-made download for each platform
# the installers support, so no user's install ever needs a compiler.
# macOS 12 is the oldest release the installer accepts (see macos/distribution.xml).
export MACOSX_DEPLOYMENT_TARGET=12.0
for platform in aarch64-apple-darwin x86_64-apple-darwin x86_64-pc-windows-msvc; do
    tmp="$(mktemp -d)"
    UV_NO_CONFIG=1 uv pip install --dry-run --quiet --only-binary :all: \
        --python-platform "$platform" --python-version "$PYTHON_VERSION" \
        --target "$tmp" --requirement installer/requirements.txt
    rm -rf "$tmp"
    echo "ok: $platform"
done
