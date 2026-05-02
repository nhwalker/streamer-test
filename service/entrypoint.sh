#!/bin/bash
# entrypoint.sh -- desktop-stream-service bootstrap.
#
# Starts services in order and waits on all of them:
#   1. gst-webrtc-signalling-server x3  (background, :SIGNALLING_PORT, +1, +2)
#   2. web_server.py                    (background, :WEB_PORT, serves /var/www/html)
#   3. pipeline.py                      (background, host or caster mode, serves browsers)
#
# Mode is selected by CASTER_HOST:
#   CASTER_HOST set   -> caster mode: ingest stream from a remote caster via webrtcsrc
#   CASTER_HOST empty -> host mode:   capture X11 directly via ximagesrc
set -euo pipefail

mkdir -p "${ARCHIVE_DIR}"

SIG_PORT_TOP=$((SIGNALLING_PORT + 1))
SIG_PORT_BOTTOM=$((SIGNALLING_PORT + 2))

# ── GPU pre-flight ────────────────────────────────────────────────────────────
if command -v nvidia-smi &>/dev/null; then
    echo "[service] NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
        || echo "  (nvidia-smi present but query failed)"
else
    echo "[service] No NVIDIA GPU detected (software decode + encode will be used)."
fi

# ── Mode selection ────────────────────────────────────────────────────────────
if [ -z "${CASTER_HOST:-}" ]; then
    echo "[service] Mode: host (X11 direct capture on ${DISPLAY})"
    if ! gst-launch-1.0 ximagesrc num-buffers=1 ! fakesink sync=false 2>/dev/null; then
        echo "[service] ERROR: Cannot access X display '${DISPLAY}'."
        echo "  * On the host run:  xhost +local:docker"
        echo "  * Run container with: -e DISPLAY=\$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix:ro"
        echo "  * If using Xauthority: -v \"\$HOME/.Xauthority:/root/.Xauthority:ro\" -e XAUTHORITY=/root/.Xauthority"
        exit 1
    fi
    echo "[service] X11 display OK."
else
    echo "[service] Mode: caster (ingest from ${CASTER_HOST}:${CASTER_SIGNALLING_PORT})"
fi

# ── Signalling servers (full / top / bottom) ─────────────────────────────────
SIG_PIDS=()
for PORT in "${SIGNALLING_PORT}" "${SIG_PORT_TOP}" "${SIG_PORT_BOTTOM}"; do
    echo "[service] Starting signalling server on ${SIGNALLING_HOST}:${PORT} ..."
    gst-webrtc-signalling-server \
        --host "${SIGNALLING_HOST}" \
        --port "${PORT}" &
    SIG_PIDS+=($!)
done

PIPPID=""

trap 'echo "[service] Shutting down..."; kill "${SIG_PIDS[@]}" 2>/dev/null; [ -n "${PIPPID}" ] && kill "${PIPPID}" 2>/dev/null; exit' \
     EXIT INT TERM

# Readiness probe -- wait up to 2 s per signalling server.
for PORT in "${SIGNALLING_PORT}" "${SIG_PORT_TOP}" "${SIG_PORT_BOTTOM}"; do
    READY=0
    for i in $(seq 1 20); do
        if nc -z 127.0.0.1 "${PORT}" 2>/dev/null; then
            READY=1
            break
        fi
        sleep 0.1
    done
    if [ "${READY}" -eq 0 ]; then
        echo "[service] ERROR: Signalling server :${PORT} did not become ready within 2 s."
        exit 1
    fi
    echo "[service] Signalling server :${PORT} ready."
done

# ── Web server ───────────────────────────────────────────────────────────────
echo "[service] Starting web server on port ${WEB_PORT} ..."
python3 /usr/local/bin/web_server.py &

# ── Access info ──────────────────────────────────────────────────────────────
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│  Desktop Stream Service ready                       │"
echo "│                                                     │"
echo "│  Full stream  : http://${HOST_IP}:${WEB_PORT}/      "
echo "│  Top half     : http://${HOST_IP}:${WEB_PORT}/top   "
echo "│  Bottom half  : http://${HOST_IP}:${WEB_PORT}/bottom"
echo "│                                                     │"
echo "│  Signalling / : ws://${HOST_IP}:${SIGNALLING_PORT}  "
echo "│  Signalling /top    : ws://${HOST_IP}:${SIG_PORT_TOP}    "
echo "│  Signalling /bottom : ws://${HOST_IP}:${SIG_PORT_BOTTOM} "
if [ -z "${CASTER_HOST:-}" ]; then
echo "│  Ingest    : X11 display ${DISPLAY} (host mode)       "
else
echo "│  Ingest    : ws://${CASTER_HOST}:${CASTER_SIGNALLING_PORT} (caster)  "
fi
echo "│  Archive   : ${ARCHIVE_DIR}                         "
echo "└─────────────────────────────────────────────────────┘"
echo ""

# ── Pipeline ─────────────────────────────────────────────────────────────────
python3 -u /usr/local/bin/pipeline.py &
PIPPID=$!

wait
