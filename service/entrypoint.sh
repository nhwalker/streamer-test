#!/bin/bash
# entrypoint.sh -- desktop-stream-service bootstrap.
#
# Starts three services in order and waits on all of them:
#   1. gst-webrtc-signalling-server  (background, :SIGNALLING_PORT)
#   2. python3 -m http.server        (background, :WEB_PORT, serves /var/www/html)
#   3. pipeline.py                   (background, RTP -> tee -> archive + WebRTC)
set -euo pipefail

# ── Config sanity ────────────────────────────────────────────────────────────
if [ -z "${DESKTOP_HOST:-}" ]; then
    echo "[service] WARNING: DESKTOP_HOST not set; RTCP bitrate feedback to caster disabled."
fi

mkdir -p "${ARCHIVE_DIR}"

# ── GPU pre-flight ────────────────────────────────────────────────────────────
# nvidia-container-toolkit injects nvidia-smi when --gpus is passed.
if command -v nvidia-smi &>/dev/null; then
    echo "[service] NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
        || echo "  (nvidia-smi present but query failed)"
else
    echo "[service] No NVIDIA GPU detected (software decode + encode will be used)."
fi

# ── Signalling server ────────────────────────────────────────────────────────
echo "[service] Starting signalling server on ${SIGNALLING_HOST}:${SIGNALLING_PORT} ..."
gst-webrtc-signalling-server \
    --host "${SIGNALLING_HOST}" \
    --port "${SIGNALLING_PORT}" &
SIGPID=$!
PIPPID=""

trap 'echo "[service] Shutting down..."; kill "${SIGPID}" 2>/dev/null; [ -n "${PIPPID}" ] && kill "${PIPPID}" 2>/dev/null; exit' \
     EXIT INT TERM

# Readiness probe -- wait up to 2 s for the signalling server.
READY=0
for i in $(seq 1 20); do
    if nc -z 127.0.0.1 "${SIGNALLING_PORT}" 2>/dev/null; then
        READY=1
        break
    fi
    sleep 0.1
done
if [ "${READY}" -eq 0 ]; then
    echo "[service] ERROR: Signalling server did not become ready within 2 s."
    exit 1
fi
echo "[service] Signalling server ready."

# ── Web server ───────────────────────────────────────────────────────────────
echo "[service] Starting web server on port ${WEB_PORT} ..."
python3 -m http.server --directory /var/www/html "${WEB_PORT}" &

# ── Access info ──────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│  Desktop Stream Service ready                       │"
echo "│                                                     │"
echo "│  Web page  : http://${HOST_IP}:${WEB_PORT}          "
echo "│  Signalling: ws://${HOST_IP}:${SIGNALLING_PORT}     "
echo "│  Caster    : rtp://${DESKTOP_HOST:-unknown}:${RTP_PORT} (RTCP feedback)  "
echo "│  Archive   : ${ARCHIVE_DIR}                         "
echo "└─────────────────────────────────────────────────────┘"
echo ""

# ── Pipeline ─────────────────────────────────────────────────────────────────
python3 -u /usr/local/bin/pipeline.py &
PIPPID=$!

wait
