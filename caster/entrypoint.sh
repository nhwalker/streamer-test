#!/bin/bash
# entrypoint.sh -- desktop-caster bootstrap.
#
# 1. Verify SERVICE_HOST is set (required for RTP push destination).
# 2. Verify the X11 display is reachable (ximagesrc silently produces black
#    frames when it cannot open the display -- catch this up front).
# 3. Log GPU info if nvidia-container-toolkit injected nvidia-smi.
# 4. exec pipeline.py.
set -euo pipefail

if [ -z "${SERVICE_HOST:-}" ]; then
    echo "[caster] ERROR: SERVICE_HOST is required (IP/hostname of the service)"
    exit 1
fi

echo "[caster] Checking X11 display: ${DISPLAY}"
if ! gst-launch-1.0 ximagesrc num-buffers=1 ! fakesink sync=false 2>/dev/null; then
    echo "[caster] ERROR: Cannot access X display '${DISPLAY}'."
    echo "  * On the host run:  xhost +local:docker"
    echo "  * Run container with: -e DISPLAY=\$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix:ro"
    echo "  * If using Xauthority: -v \"\$HOME/.Xauthority:/root/.Xauthority:ro\" -e XAUTHORITY=/root/.Xauthority"
    exit 1
fi
echo "[caster] X11 display OK."

if command -v nvidia-smi &>/dev/null; then
    echo "[caster] NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
        || echo "  (nvidia-smi present but query failed)"
else
    echo "[caster] No NVIDIA GPU detected (software encoding will be used)."
fi

echo "[caster] Pushing RTP to ${SERVICE_HOST}:${RTP_PORT}"
exec python3 -u /usr/local/bin/pipeline.py
