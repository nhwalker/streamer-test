#!/bin/bash
# entrypoint.sh — starts three services in order, then watches over all of them
#
#  1. gst-webrtc-signalling-server  (background, WebSocket :SIGNALLING_PORT)
#  2. python3 -m http.server        (background, HTTP :WEB_PORT, serves web/)
#  3. pipeline.py                   (background, GStreamer capture loop)
#
# All three run as background jobs so a GStreamer pipeline crash does not kill
# the HTTP or signalling servers.  The main process waits for all jobs and
# handles SIGTERM/SIGINT cleanly.
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
PIPPID=""

# Kill all tracked background processes on exit/interrupt.
# PIPPID may be empty until the pipeline block runs, so guard the kill.
trap 'echo "[entrypoint] Shutting down..."; kill "${SIGPID}" 2>/dev/null; [ -n "${PIPPID}" ] && kill "${PIPPID}" 2>/dev/null; exit' \
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

# ── GStreamer pipeline ────────────────────────────────────────────────────────
# pipeline.py is a Python wrapper around the same gst-launch pipeline that
# also connects the webrtcbin-ready signal to configure a TURN server per
# consumer — webrtcsink 0.13.x does not expose a turn-server property.
python3 -u /usr/local/bin/pipeline.py &
PIPPID=$!

# Wait for all background jobs.  Returns when every job has exited, or when
# the trap fires on SIGTERM/SIGINT and calls exit.
wait
