#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DISPLAY:-}" ]]; then
    echo "entrypoint: DISPLAY is unset." >&2
    echo "Pass -e DISPLAY=:0 and bind-mount /tmp/.X11-unix from the host." >&2
    exit 1
fi

if [[ ! -S "/tmp/.X11-unix/X${DISPLAY##*:}" ]] \
   && [[ ! -S "/tmp/.X11-unix/X${DISPLAY##:}" ]]; then
    echo "entrypoint: warning — no X11 socket found under /tmp/.X11-unix for DISPLAY=${DISPLAY}." >&2
    echo "  Did you forget -v /tmp/.X11-unix:/tmp/.X11-unix ?" >&2
fi

if command -v vainfo >/dev/null 2>&1; then
    echo "── vainfo (LIBVA_DRIVER_NAME=${LIBVA_DRIVER_NAME:-unset}) ────────────────"
    vainfo 2>&1 | sed -n '1,40p' || echo "vainfo failed; continuing"
    echo "──────────────────────────────────────────────────────────────────────────"
fi

cd /opt/viewer
exec node_modules/.bin/electron . "$@"
