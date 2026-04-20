#!/bin/bash
# pipeline.sh — builds and exec's the GStreamer desktop capture pipeline
#
# Environment variables (all have defaults from Dockerfile ENV):
#   DISPLAY               X11 display to capture         (:0)
#   STREAM_CODEC          vp9 | vp8 | h264 | h265        (vp9)
#   STREAM_WIDTH          capture width in pixels         (1920)
#   STREAM_HEIGHT         capture height in pixels        (1080)
#   STREAM_FRAMERATE      frames per second               (30)
#   STREAM_BITRATE_KBPS   target bitrate in kbps          (2000)
#   SIGNALLING_PORT       port of the signalling server   (8443)
#   GST_WEBRTC_STUN_SERVER optional STUN URI              ("")
#   GST_WEBRTC_TURN_SERVER optional TURN URI              ("")
#                           Format: turn://user:pass@host:port
#
# ── Wayland/PipeWire swap ─────────────────────────────────────────────────────
# When migrating from X11 to Wayland, replace the ximagesrc block with:
#
#   pipewiresrc path="${PIPEWIRE_NODE_ID:-0}" do-timestamp=true \
#
# Everything downstream (videorate, videoscale, webrtcsink, signalling, web page)
# remains unchanged — only the source element changes.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Codec → GStreamer video caps ──────────────────────────────────────────────
case "${STREAM_CODEC:-vp9}" in
    vp9)  VIDEO_CAPS="video/x-vp9" ;;
    vp8)  VIDEO_CAPS="video/x-vp8" ;;
    h264) VIDEO_CAPS="video/x-h264" ;;
    h265) VIDEO_CAPS="video/x-h265" ;;
    *)
        echo "[pipeline] ERROR: Unknown STREAM_CODEC '${STREAM_CODEC}'. Use vp9, vp8, h264, or h265."
        exit 1
        ;;
esac

# ── GPU encoder detection ─────────────────────────────────────────────────────
# Log whether NVENC hardware encoders are available in the GStreamer registry.
# webrtcsink auto-selects the highest-ranked encoder for the chosen codec, so
# hardware encoders (nvh264enc, nvh265enc) are preferred when present.
if gst-inspect-1.0 nvh264enc &>/dev/null; then
    echo "[pipeline] NVIDIA NVENC detected: hardware encoding available (nvh264enc, nvh265enc)"
else
    echo "[pipeline] NVIDIA NVENC not detected: software encoding will be used"
fi

# ── Optional STUN server property ────────────────────────────────────────────
# Needed when browser and container are NOT on the same host and --network=host
# is not in use.  Example: GST_WEBRTC_STUN_SERVER=stun://stun.l.google.com:19302
STUN_PROP=""
if [ -n "${GST_WEBRTC_STUN_SERVER:-}" ]; then
    STUN_PROP="stun-server=${GST_WEBRTC_STUN_SERVER}"
fi

TURN_PROP=""
if [ -n "${GST_WEBRTC_TURN_SERVER:-}" ]; then
    TURN_PROP="turn-server=${GST_WEBRTC_TURN_SERVER}"
fi

# ── GStreamer caps strings ────────────────────────────────────────────────────
RATE_CAPS="video/x-raw,framerate=${STREAM_FRAMERATE}/1"
SIZE_CAPS="video/x-raw,width=${STREAM_WIDTH},height=${STREAM_HEIGHT}"
BITRATE=$(( ${STREAM_BITRATE_KBPS:-2000} * 1000 ))

echo "[pipeline] Starting capture:"
echo "  Display    : ${DISPLAY}"
echo "  Resolution : ${STREAM_WIDTH}x${STREAM_HEIGHT} @ ${STREAM_FRAMERATE} fps"
echo "  Codec      : ${VIDEO_CAPS} @ ${STREAM_BITRATE_KBPS} kbps"
echo "  Signalling : ws://127.0.0.1:${SIGNALLING_PORT}"
[ -n "${STUN_PROP}" ] && echo "  STUN       : ${GST_WEBRTC_STUN_SERVER}"
[ -n "${TURN_PROP}" ] && echo "  TURN       : ${GST_WEBRTC_TURN_SERVER}"

# ── Launch pipeline ───────────────────────────────────────────────────────────
# -e  : send EOS on interrupt so the pipeline shuts down cleanly
#
# Pipeline:
#   ximagesrc      capture the X11 root window
#   videorate      enforce the target frame rate (drops/duplicates frames)
#   videoscale     rescale to the requested resolution
#   videoconvert   ensure the pixel format webrtcsink expects
#   webrtcsink     WebRTC encode + send; negotiates codec with each browser peer
#
# webrtcsink properties:
#   signaller::uri   points at the standalone signalling server started by entrypoint.sh
#   video-caps       constrains codec selection during SDP negotiation
exec gst-launch-1.0 -e \
    ximagesrc display-name="${DISPLAY}" use-damage=false \
    ! videorate \
    ! "${RATE_CAPS}" \
    ! videoscale \
    ! "${SIZE_CAPS}" \
    ! videoconvert \
    ! queue \
    ! webrtcsink name=ws \
        "signaller::uri=ws://127.0.0.1:${SIGNALLING_PORT}" \
        "video-caps=${VIDEO_CAPS}" \
        ${STUN_PROP} \
        ${TURN_PROP}
