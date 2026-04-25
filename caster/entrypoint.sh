#!/bin/bash
# entrypoint.sh -- desktop-caster bootstrap.
#
# 1. Verify the X11 display is reachable.
# 2. Log GPU info if nvidia-container-toolkit injected nvidia-smi.
# 3. Start the WebRTC signalling server (service connects to it via webrtcsrc).
# 4. exec pipeline.py.
set -euo pipefail

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

# ── Signalling server ─────────────────────────────────────────────────────────
# The service's webrtcsrc connects to this server to receive the stream.
echo "[caster] Starting signalling server on ${SIGNALLING_HOST}:${SIGNALLING_PORT} ..."
gst-webrtc-signalling-server \
    --host "${SIGNALLING_HOST}" \
    --port "${SIGNALLING_PORT}" &
SIGPID=$!

trap 'echo "[caster] Shutting down..."; kill "${SIGPID}" 2>/dev/null; exit' \
     EXIT INT TERM

# Readiness probe — wait up to 2 s for the signalling server.
READY=0
for i in $(seq 1 20); do
    if nc -z 127.0.0.1 "${SIGNALLING_PORT}" 2>/dev/null; then
        READY=1
        break
    fi
    sleep 0.1
done
if [ "${READY}" -eq 0 ]; then
    echo "[caster] ERROR: Signalling server did not become ready within 2 s."
    exit 1
fi
echo "[caster] Signalling server ready."
echo "[caster] Service should connect webrtcsrc to ws://CASTER_HOST:${SIGNALLING_PORT}"

exec python3 -u /usr/local/bin/pipeline.py
