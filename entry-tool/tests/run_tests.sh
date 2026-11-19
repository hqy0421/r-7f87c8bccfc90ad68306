#!/usr/bin/env bash
# Run the end-to-end tests with the private (rootless) Chromium install.
set -euo pipefail

AUTOMATION="${AUTOMATION:-/var/tmp/automation}"
export PYTHONPATH="$AUTOMATION/lib${PYTHONPATH:+:$PYTHONPATH}"
export PLAYWRIGHT_BROWSERS_PATH="$AUTOMATION/browsers"
export LD_LIBRARY_PATH="$AUTOMATION/sysroot/usr/lib/x86_64-linux-gnu:$AUTOMATION/sysroot/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

cd "$(dirname "$0")/../.."
exec python3 -m unittest entry-tool.tests.test_entry "$@"
