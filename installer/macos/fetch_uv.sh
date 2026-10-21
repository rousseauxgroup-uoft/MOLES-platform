#!/bin/bash
# Downloads uv for both Mac chips and joins them into one binary that runs
# natively on Intel and Apple Silicon.
#     installer/macos/fetch_uv.sh <uv version> <output path>
set -euo pipefail

UV_VERSION="$1"
OUT="$2"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

for target in aarch64-apple-darwin x86_64-apple-darwin; do
    curl --fail --silent --show-error --location \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$target.tar.gz" \
        | tar -xz -C "$WORK"
done
lipo -create "$WORK/uv-aarch64-apple-darwin/uv" "$WORK/uv-x86_64-apple-darwin/uv" -output "$OUT"
chmod 755 "$OUT"
lipo -info "$OUT"
