#!/bin/bash
# entrypoint.sh -- desktop-stream-service bootstrap.
#
# Starts services in order and waits on all of them:
#   1. desktop_config.py                (writes /run/desktop-stream/config.json)
#   2. gst-webrtc-signalling-server xN  (one per configured screen + full-frame)
#   3. web_server.py                    (background, :WEB_PORT, serves /var/www/html)
#   4. pipeline.py                      (background, captures X11 via ximagesrc)
set -euo pipefail

mkdir -p "${ARCHIVE_DIR}" "${ARCHIVE_LIVE_DIR}"

# ── GPU pre-flight ────────────────────────────────────────────────────────────
if command -v nvidia-smi &>/dev/null; then
    echo "[service] NVIDIA GPU detected:"
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader 2>/dev/null \
        || echo "  (nvidia-smi present but query failed)"
else
    echo "[service] No NVIDIA GPU detected (software decode + encode will be used)."
fi

# ── X11 pre-flight ────────────────────────────────────────────────────────────
echo "[service] X11 direct capture on ${DISPLAY}"
if ! gst-launch-1.0 ximagesrc num-buffers=1 ! fakesink sync=false 2>/dev/null; then
    echo "[service] ERROR: Cannot access X display '${DISPLAY}'."
    echo "  * On the host run:  xhost +local:docker"
    echo "  * Run container with: -e DISPLAY=\$DISPLAY -v /tmp/.X11-unix:/tmp/.X11-unix:ro"
    echo "  * If using Xauthority: -v \"\$HOME/.Xauthority:/root/.Xauthority:ro\" -e XAUTHORITY=/root/.Xauthority"
    exit 1
fi
echo "[service] X11 display OK."

# ── Compute and persist the runtime config (desktop name, resolution, splits) ─
# pipeline.py and web_server.py both read /run/desktop-stream/config.json so
# they agree on screen names, ports, and crop regions.
mkdir -p /run/desktop-stream
python3 /usr/local/bin/desktop_config.py >/dev/null

# Read back the screen list so the rest of the script can spin up one
# signalling server per (stream × tier) pair.  Tiers come from
# WEBRTC_SCALE_LADDER (see desktop_config.py); the default ladder produces
# 4 tiers per stream.  Legacy callers that don't know about tiers still
# work because tier 0 keeps the same port as the pre-ladder design.
mapfile -t SIG_PORTS < <(python3 -c '
import json
with open("/run/desktop-stream/config.json") as fh:
    cfg = json.load(fh)
for t in cfg.get("fullTiers") or [{"signallingPort": cfg["fullSignallingPort"]}]:
    print(t["signallingPort"])
for s in cfg["screens"]:
    for t in s.get("tiers") or [{"signallingPort": s["signallingPort"]}]:
        print(t["signallingPort"])
')

# ── Signalling servers (one per configured stream) ───────────────────────────
SIG_PIDS=()
for PORT in "${SIG_PORTS[@]}"; do
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
for PORT in "${SIG_PORTS[@]}"; do
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
python3 - "${HOST_IP}" "${WEB_PORT}" <<'PYEOF'
import json, sys
host_ip, web_port = sys.argv[1], sys.argv[2]
with open("/run/desktop-stream/config.json") as fh:
    cfg = json.load(fh)
print(f"│  Desktop name : {cfg['desktopName']}")
print(f"│  Resolution   : {cfg['width']}x{cfg['height']}")
print(f"│  Full stream  : http://{host_ip}:{web_port}/")
for s in cfg["screens"]:
    print(f"│    {s['name']:<10s}: http://{host_ip}:{web_port}{s['path']}"
          f"  (ws :{s['signallingPort']})")
print(f"│  Signalling / : ws://{host_ip}:{cfg['fullSignallingPort']}")
# Tier count is the same for every stream; print it once.
tier_count = len(cfg.get("fullTiers") or [None])
if tier_count > 1:
    print(f"│  Tiers/stream : {tier_count} (browser auto-picks by viewport)")
PYEOF
echo "│  Ingest    : X11 display ${DISPLAY}                  "
echo "│  Archive   : ${ARCHIVE_DIR} (live: ${ARCHIVE_LIVE_DIR})"
echo "└─────────────────────────────────────────────────────┘"
echo ""

# ── Pipeline ─────────────────────────────────────────────────────────────────
python3 -u /usr/local/bin/pipeline.py &
PIPPID=$!

wait
