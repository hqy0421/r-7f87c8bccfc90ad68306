#!/usr/bin/env bash
# Install Playwright + Chromium **without root**, into a private prefix, and
# extract the Debian libraries Chromium needs into a private sysroot.
#
# This sandbox has no root and no apt, so the usual `playwright install-deps`
# cannot run.  The script below reproduces exactly what was done here:
#
#   * the Python package goes to $AUTOMATION/lib (via pip --target)
#   * the browser bundle goes to $AUTOMATION/browsers
#   * the .deb files are unpacked to $AUTOMATION/sysroot and picked up
#     through LD_LIBRARY_PATH at run time
#
# On a normal machine with root, `python3 -m playwright install --with-deps
# chromium` is enough and this script is unnecessary (it still works).
set -euo pipefail

AUTOMATION="${AUTOMATION:-/var/tmp/automation}"
DEB_RELEASE="${DEB_RELEASE:-bookworm}"
mkdir -p "$AUTOMATION"/{lib,browsers,deb/debs,sysroot}

if [ ! -d "$AUTOMATION/lib/playwright" ]; then
  python3 -m pip install --quiet --target "$AUTOMATION/lib" playwright
fi

export PYTHONPATH="$AUTOMATION/lib${PYTHONPATH:+:$PYTHONPATH}"
export PLAYWRIGHT_BROWSERS_PATH="$AUTOMATION/browsers"
python3 -m playwright install chromium

# Chromium's shared-library dependencies, fetched as archives rather than
# installed, and unpacked next to each other in a private sysroot.
if [ ! -e "$AUTOMATION/sysroot/usr/lib/x86_64-linux-gnu/libatk-1.0.so.0" ]; then
  if [ -z "${SKIP_DEBS:-}" ]; then
    cat >"$AUTOMATION/deb/fetch.py" <<'PY'
import gzip, json, os, re, subprocess, sys, urllib.request
from urllib.parse import urljoin

root = sys.argv[1]
mirror = f"https://deb.debian.org/debian/dists/{sys.argv[2]}/main/binary-amd64/"
packages_gz = urllib.request.urlopen(urljoin(mirror, "Packages.gz")).read()
text = gzip.decompress(packages_gz).decode()
index = {}
for block in text.split("\n\n"):
    name = re.search(r"^Package: (\S+)", block, re.M)
    file_ = re.search(r"^Filename: (\S+)", block, re.M)
    if name and file_:
        index[name.group(1)] = file_.group(1)
with open(os.path.join(root, "pkgs.json"), "w") as fh:
    json.dump(index, fh, indent=1, sort_keys=True)
PY
    python3 "$AUTOMATION/deb/fetch.py" "$AUTOMATION/deb" "$DEB_RELEASE"

    # The list of libraries Chromium's own ldd output asks for.  Resolve
    # them transitively by iterating: unpack, re-check, repeat.
    for _ in $(seq 1 6); do
      ldd "$(find "$AUTOMATION/browsers" -name chrome -type f | head -1)" 2>/dev/null \
        | awk '/not found/{print $1}' | sed 's/\.so.*//' | sort -u >"$AUTOMATION/deb/missing.txt"
      [ -s "$AUTOMATION/deb/missing.txt" ] || break
      while read -r lib; do
        pkg=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" \
              "$AUTOMATION/deb/pkgs.json" "$lib")
        [ -n "$pkg" ] || continue
        base=$(basename "$pkg")
        [ -f "$AUTOMATION/deb/debs/$base" ] && continue
        curl -fsSL "https://deb.debian.org/debian/$pkg" -o "$AUTOMATION/deb/debs/$base"
      done <"$AUTOMATION/deb/missing.txt"
      for f in "$AUTOMATION/deb/debs"/*.deb; do
        dpkg-deb -x "$f" "$AUTOMATION/sysroot" 2>/dev/null || {
          ar x "$f" --output "$AUTOMATION/deb/unpack" 2>/dev/null ||
            (mkdir -p "$AUTOMATION/deb/unpack" && cd "$AUTOMATION/deb/unpack" && ar x "$f")
          tar -xf "$AUTOMATION/deb/unpack/data.tar"* -C "$AUTOMATION/sysroot"
        }
      done
    done
  fi
fi

echo "done:"
echo "  PYTHONPATH=$AUTOMATION/lib"
echo "  PLAYWRIGHT_BROWSERS_PATH=$AUTOMATION/browsers"
echo "  LD_LIBRARY_PATH=$AUTOMATION/sysroot/usr/lib/x86_64-linux-gnu:$AUTOMATION/sysroot/lib/x86_64-linux-gnu"
