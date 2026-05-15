#!/usr/bin/env bash
# Thin shim: just exec Firefox against whatever X server / pulse server the
# host wired in (DISPLAY, /tmp/.X11-unix, PULSE_SERVER, etc.). Set VERBOSE=1
# to print `vainfo` first — useful for confirming NVDEC came up.
set -eu

if [ -n "${VERBOSE:-}" ] && command -v vainfo >/dev/null 2>&1; then
    echo "[entrypoint] vainfo ↓"
    vainfo 2>&1 | sed 's/^/  /' || true
fi

exec "$@"
