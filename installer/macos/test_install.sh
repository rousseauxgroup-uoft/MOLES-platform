#!/bin/bash
# Installs a built MOLES.pkg the way a user would (into /Applications, needs
# sudo) and checks the result. Meant for CI machines, not your own Mac.
#     installer/macos/test_install.sh dist/MOLES-<version>.pkg <arm64|x86_64>
set -euo pipefail

PKG="$1"
EXPECTED_ARCH="$2"
APP=/Applications/MOLES.app
PYTHON="$APP/Contents/Resources/runtime/bin/python3"

status=0
sudo installer -pkg "$PKG" -target / || status=$?
tail -n 15 /Library/Logs/MOLES-install.log || true
[ $status -eq 0 ] || exit $status

"$PYTHON" - "$EXPECTED_ARCH" <<'EOF'
import platform, sys
import moles, moles.updates as u
import PyQt5.QtWidgets, numpy, scipy, pandas, matplotlib, pyqtgraph, serial
assert u.installed_by_installer(), "install marker missing"
assert platform.machine() == sys.argv[1], f"{platform.machine()} Python, expected {sys.argv[1]}"
print("MOLES", moles.__version__, "installed for", platform.machine())
EOF

# The app must open its launcher; run it without a screen for a few seconds.
QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/MOLES" &
pid=$!
sleep 8
kill -0 "$pid"  # still running = the launcher started
kill "$pid"
echo "Launcher started"
