#!/usr/bin/env bash
# Bring up a minimal X11 + dbus + pulseaudio session so Firefox can run, then
# exec whatever the user asked for (default: firefox). Skip any of the three
# by exporting SKIP_XVFB / SKIP_DBUS / SKIP_PULSE — e.g. when you're mounting
# the host's /tmp/.X11-unix or piping in PULSE_SERVER.
set -eu

: "${DISPLAY:=:99}"
: "${XVFB_GEOMETRY:=1920x1080x24}"
export DISPLAY

if [ -z "${SKIP_XVFB:-}" ] && ! xdpyinfo >/dev/null 2>&1; then
    echo "[entrypoint] starting Xvfb on ${DISPLAY} (${XVFB_GEOMETRY})"
    Xvfb "${DISPLAY}" -screen 0 "${XVFB_GEOMETRY}" \
         -ac -nolisten tcp +extension GLX +extension RANDR +render -noreset &
    for _ in $(seq 1 100); do
        xdpyinfo >/dev/null 2>&1 && break
        sleep 0.05
    done
    xdpyinfo >/dev/null 2>&1 || { echo "[entrypoint] Xvfb failed to start" >&2; exit 1; }
fi

if [ -z "${SKIP_DBUS:-}" ] && [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    eval "$(dbus-launch --sh-syntax)"
    export DBUS_SESSION_BUS_ADDRESS DBUS_SESSION_BUS_PID
fi

if [ -z "${SKIP_PULSE:-}" ] && [ -z "${PULSE_SERVER:-}" ] \
   && ! pactl info >/dev/null 2>&1; then
    pulseaudio --start --exit-idle-time=-1 --log-target=stderr \
               >/dev/null 2>&1 || true
fi

# Sanity log: which VAAPI driver actually loaded? `vainfo` only prints H264
# entrypoints when nvidia-vaapi-driver successfully dlopen'd libcuda. Useful
# for verifying the NVIDIA Container Toolkit wired things up correctly.
if [ -n "${VERBOSE:-}" ] && command -v vainfo >/dev/null 2>&1; then
    echo "[entrypoint] vainfo ↓"
    vainfo 2>&1 | sed 's/^/  /' || true
fi

exec "$@"
