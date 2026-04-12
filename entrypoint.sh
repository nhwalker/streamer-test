#!/bin/bash
# entrypoint.sh — starts three services in order, then hands off to the pipeline
#
#  1. gst-webrtc-signalling-server  (background, WebSocket :SIGNALLING_PORT)
#  2. python3 -m http.server        (background, HTTP :WEB_PORT, serves web/)
#  3. pipeline.sh                   (foreground via exec, GStreamer capture loop)
#
# All env vars have defaults set in the Dockerfile ENV block.
set -euo pipefail

# ── X11 pre-flight ────────────────────────────────────────────────────────────
# ximagesrc silently produces black frames when it cannot open the display.
# Catch this early with a single-buffer probe.
echo "[entrypoint] Checking X11 display: ${DISPLAY}"
if ! gst-launch-1.0 ximagesrc num-buffers=1 ! fakesink sync=false 2>/dev/null; then
    echo "[entrypoint] ERROR: Cannot access X display '${DISPLAY}'."
    echo "  • On the host run:  xhost +local:docker"
    echo "  • Run container with: -e DISPLAY=\$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix:ro"
    echo "  • If using Xauthority: -v \"\$HOME/.Xauthority:/root/.Xauthority:ro\" -e XAUTHORITY=/root/.Xauthority"
    exit 1
fi
echo "[entrypoint] X11 display OK."

# ── GPU pre-flight ────────────────────────────────────────────────────────────
# nvidia-container-toolkit injects nvidia-smi when the container runs with --gpus.
# Log GPU info early so operators can confirm the GPU was injected correctly.
if command -v nvidia-smi &>/dev/null; then
    echo "[entrypoint] NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
        || echo "  (nvidia-smi present but query failed)"
else
    echo "[entrypoint] No NVIDIA GPU detected (software encoding will be used)."
fi

# ── Signalling server ─────────────────────────────────────────────────────────
echo "[entrypoint] Starting signalling server on ${SIGNALLING_HOST}:${SIGNALLING_PORT} ..."

# NOTE: Verify --host / --port flag names with:
#   gst-webrtc-signalling-server --help
# If the flags differ in your gst-plugins-rs version, adjust below.
gst-webrtc-signalling-server \
    --host "${SIGNALLING_HOST}" \
    --port "${SIGNALLING_PORT}" &
SIGPID=$!

# Clean up background processes on exit/interrupt
trap 'echo "[entrypoint] Shutting down..."; kill "${SIGPID}" 2>/dev/null; exit' \
     EXIT INT TERM

# Readiness probe — wait up to 2 s for the signalling server to accept connections
READY=0
for i in $(seq 1 20); do
    if nc -z 127.0.0.1 "${SIGNALLING_PORT}" 2>/dev/null; then
        READY=1
        break
    fi
    sleep 0.1
done

if [ "${READY}" -eq 0 ]; then
    echo "[entrypoint] ERROR: Signalling server did not become ready within 2 s."
    exit 1
fi
echo "[entrypoint] Signalling server ready."

# ── Web server ────────────────────────────────────────────────────────────────
# Serves /var/www/html/ which contains index.html and gstwebrtc-api/.
# Replace with nginx/CDN in production.
echo "[entrypoint] Starting web server on port ${WEB_PORT} ..."
python3 -m http.server --directory /var/www/html "${WEB_PORT}" &

# ── Access info ───────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│  Desktop Stream ready                               │"
echo "│                                                     │"
echo "│  Web page  : http://${HOST_IP}:${WEB_PORT}          "
echo "│  Signalling: ws://${HOST_IP}:${SIGNALLING_PORT}     "
echo "│                                                     │"
echo "│  Same host?  http://localhost:${WEB_PORT}           "
echo "└─────────────────────────────────────────────────────┘"
echo ""

# ── GStreamer pipeline (replaces this shell; signals propagate cleanly) ───────
exec /usr/local/bin/pipeline.sh
