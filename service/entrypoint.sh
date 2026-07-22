#!/bin/bash
# entrypoint.sh -- desktop-stream-service bootstrap.
#
# Starts services in order and waits on all of them:
#   1. desktop_config.py   (writes /run/desktop-stream/config.json)
#   2. mediamtx            (RTSP ingest on loopback, WHEP egress on :WHEP_PORT)
#   3. web_server.py       (background, :WEB_PORT, serves /var/www/html)
#   4. pipeline.py         (supervised ffmpeg: x11grab capture -> live RTSP
#                           tiers + archive segments)
set -euo pipefail

mkdir -p "${ARCHIVE_DIR}" "${ARCHIVE_LIVE_DIR}"

# ── GPU pre-flight ────────────────────────────────────────────────────────────
if command -v nvidia-smi &>/dev/null; then
    echo "[service] NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
        || echo "  (nvidia-smi present but query failed)"
else
    echo "[service] No NVIDIA GPU detected (software encoding will be used)."
fi

# ── X11 pre-flight ────────────────────────────────────────────────────────────
echo "[service] X11 direct capture on ${DISPLAY}"
if ! ffmpeg -nostdin -hide_banner -loglevel error \
        -f x11grab -video_size 64x64 -i "${DISPLAY}" \
        -frames:v 1 -f null - 2>/dev/null; then
    echo "[service] ERROR: Cannot access X display '${DISPLAY}'."
    echo "  * On the host run:  xhost +local:docker"
    echo "  * Run container with: -e DISPLAY=\$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix:ro"
    echo "  * If using Xauthority: -v \"\$HOME/.Xauthority:/root/.Xauthority:ro\" -e XAUTHORITY=/root/.Xauthority"
    exit 1
fi
echo "[service] X11 display OK."

# ── Compute and persist the runtime config (desktop name, resolution, splits) ─
# pipeline.py and web_server.py both read /run/desktop-stream/config.json so
# they agree on screen names, tier paths, and crop regions.
mkdir -p /run/desktop-stream
python3 /usr/local/bin/desktop_config.py >/dev/null

# ── MediaMTX (WebRTC/WHEP egress) ─────────────────────────────────────────────
# stream_command.py renders the config from the runtime config + env:
# loopback RTSP ingest, WHEP on :WHEP_PORT, everything else disabled.
WHEP_PORT="${WHEP_PORT:-8889}"
python3 /usr/local/bin/stream_command.py > /run/desktop-stream/mediamtx.yml
echo "[service] Starting MediaMTX (rtsp 127.0.0.1:${MEDIAMTX_RTSP_PORT}, whep :${WHEP_PORT}) ..."
mediamtx /run/desktop-stream/mediamtx.yml &
MTXPID=$!

PIPPID=""
WEBPID=""
trap 'echo "[service] Shutting down..."; [ -n "${PIPPID}" ] && kill "${PIPPID}" 2>/dev/null; [ -n "${WEBPID}" ] && kill "${WEBPID}" 2>/dev/null; kill "${MTXPID}" 2>/dev/null; exit' \
     EXIT INT TERM

# Readiness probe -- wait up to 30 s for the RTSP listener.  MediaMTX
# itself starts in milliseconds; the generous budget covers CPU-starved
# hosts (e.g. CI runners already saturated by another encoder stack).
READY=0
for i in $(seq 1 300); do
    if nc -z 127.0.0.1 "${MEDIAMTX_RTSP_PORT}" 2>/dev/null; then
        READY=1
        break
    fi
    sleep 0.1
done
if [ "${READY}" -eq 0 ]; then
    echo "[service] ERROR: MediaMTX did not become ready within 30 s."
    if kill -0 "${MTXPID}" 2>/dev/null; then
        echo "[service] MediaMTX process is still running but not accepting RTSP connections."
    else
        echo "[service] MediaMTX process has EXITED (see its log lines above — likely a config or port-bind error)."
    fi
    echo "[service] ---- generated mediamtx.yml ----"
    cat /run/desktop-stream/mediamtx.yml
    echo "[service] ---- end mediamtx.yml ----"
    exit 1
fi
echo "[service] MediaMTX ready."

# ── Web server ───────────────────────────────────────────────────────────────
# NOTE: the functional-test harness (ServiceStack.java) waits for this exact
# "web server on port" log line — keep it if you reword the message.
echo "[service] Starting web server on port ${WEB_PORT} ..."
python3 /usr/local/bin/web_server.py &
WEBPID=$!

# ── Access info ──────────────────────────────────────────────────────────────
# `|| true` guards set -e/pipefail: hostname may be missing from the image
# or fail entirely; ${HOST_IP:-} then also covers empty output.
HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
HOST_IP="${HOST_IP:-localhost}"
echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│  Desktop Stream Service ready                       │"
python3 - "${HOST_IP}" "${WEB_PORT}" <<'PYEOF'
import json, sys
host_ip, web_port = sys.argv[1], sys.argv[2]
with open("/run/desktop-stream/config.json") as fh:
    cfg = json.load(fh)
print(f"│  Desktop name : {cfg['desktopName']}")
print(f"│  Resolution   : {cfg['width']}x{cfg['height']}")
print(f"│  Full stream  : http://{host_ip}:{web_port}/")
for s in cfg["screens"]:
    print(f"│    {s['name']:<10s}: http://{host_ip}:{web_port}{s['path']}")
print(f"│  WHEP         : http://{host_ip}:{cfg['webrtcPort']}/<path>/whep")
tier_count = len(cfg.get("fullTiers") or [None])
if tier_count > 1:
    print(f"│  Tiers/stream : {tier_count} (browser auto-picks by viewport)")
PYEOF
echo "│  Ingest    : X11 display ${DISPLAY}                  "
case "${ARCHIVE_ENABLED:-1}" in
    0|false|no|off|FALSE|No|NO|Off|OFF|False)
        echo "│  Archive   : disabled (ARCHIVE_ENABLED=${ARCHIVE_ENABLED:-})" ;;
    *)  echo "│  Archive   : ${ARCHIVE_DIR} (live: ${ARCHIVE_LIVE_DIR})" ;;
esac
echo "└─────────────────────────────────────────────────────┘"
echo ""

# ── Pipeline (supervised ffmpeg) ─────────────────────────────────────────────
python3 -u /usr/local/bin/pipeline.py &
PIPPID=$!

wait
