#!/bin/bash
# Builds dist/MOLES-<version>.pkg, the macOS installer (Intel + Apple Silicon).
#     installer/macos/build_pkg.sh <version> <moles wheel> <universal uv binary>
# The GitHub Actions workflow runs this; fetch_uv.sh makes the uv binary.
set -euo pipefail

VERSION="$1"
WHEEL="$2"
UV_BIN="$3"
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="$(dirname "$HERE")"
REPO="$(dirname "$INSTALLER")"
IDENTIFIER="ca.utoronto.rousseauxgroup.moles.pkg"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
APP="$WORK/root/MOLES.app"
SETUP="$APP/Contents/Resources/setup"
mkdir -p "$APP/Contents/MacOS" "$SETUP" "$WORK/scripts" "$WORK/resources"

# The app bundle: a launch script, the icon, and what postinstall needs.
sed "s/@VERSION@/$VERSION/g" "$HERE/Info.plist" > "$APP/Contents/Info.plist"
install -m 755 "$HERE/MOLES" "$APP/Contents/MacOS/MOLES"
cp "$INSTALLER/assets/MOLES.icns" "$APP/Contents/Resources/"
install -m 755 "$UV_BIN" "$SETUP/uv"
install -m 755 "$HERE/install_env.sh" "$SETUP/"
cp "$WHEEL" "$INSTALLER/requirements.txt" "$INSTALLER/python-version.txt" "$SETUP/"
install -m 755 "$HERE/scripts/preinstall" "$HERE/scripts/postinstall" "$WORK/scripts/"

# Always install to /Applications/MOLES.app, even if an older copy was moved:
# that's where preinstall clears out the previous version.
pkgbuild --analyze --root "$WORK/root" "$WORK/component.plist"
plutil -replace 0.BundleIsRelocatable -bool NO "$WORK/component.plist"
plutil -replace 0.BundleIsVersionChecked -bool NO "$WORK/component.plist"
plutil -replace 0.BundleOverwriteAction -string upgrade "$WORK/component.plist"

pkgbuild --root "$WORK/root" --component-plist "$WORK/component.plist" \
    --install-location /Applications --scripts "$WORK/scripts" \
    --identifier "$IDENTIFIER" --version "$VERSION" \
    "$WORK/component-raw.pkg"

# pkgbuild gives scripts 10 minutes; the download can take longer on a slow
# connection, so allow an hour.
pkgutil --expand "$WORK/component-raw.pkg" "$WORK/component"
sed -i '' 's/timeout="600"/timeout="3600"/g' "$WORK/component/PackageInfo"
grep -q 'postinstall file="./postinstall" timeout="3600"' "$WORK/component/PackageInfo"
pkgutil --flatten "$WORK/component" "$WORK/moles-component.pkg"
rm -rf "$WORK/component" "$WORK/component-raw.pkg"

for page in welcome.html conclusion.html; do
    sed "s/@VERSION@/$VERSION/g" "$HERE/$page" > "$WORK/resources/$page"
done
cp "$REPO/LICENSE" "$WORK/resources/LICENSE.txt"
sed "s/@VERSION@/$VERSION/g" "$HERE/distribution.xml" > "$WORK/distribution.xml"

mkdir -p "$REPO/dist"
productbuild --distribution "$WORK/distribution.xml" --resources "$WORK/resources" \
    --package-path "$WORK" "$REPO/dist/MOLES-$VERSION.pkg"
echo "Built $REPO/dist/MOLES-$VERSION.pkg"
