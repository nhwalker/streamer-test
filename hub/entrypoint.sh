#!/bin/bash
set -euo pipefail

CONFIG="${WEB_DIR}/streams.json"
if [ -f "${CONFIG}" ]; then
    count=$(python3 -c "import json; print(len(json.load(open('${CONFIG}'))))" 2>/dev/null || echo "?")
    echo "[hub] streams.json loaded: ${count} stream(s)"
else
    echo "[hub] WARNING: ${CONFIG} not found — mount streams.json into ${WEB_DIR}/"
fi

echo "[hub] Starting web server on port ${WEB_PORT} ..."
exec python3 /usr/local/bin/web_server.py
