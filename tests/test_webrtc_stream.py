"""
test_webrtc_stream.py — End-to-end integration test for the GStreamer WebRTC pipeline.

Test scenario
─────────────
1. Xvfb container runs a virtual X display with the root window painted solid red.
2. GStreamer container captures display :1 via ximagesrc and streams it over WebRTC.
3. This test opens the streaming web page in a headless Chrome container, waits for
   the WebRTC video to start playing, then samples the center pixel of the video
   frame and asserts it is approximately red.

If the full pipeline is working correctly the pixel should be close to (255, 0, 0).
VP8 chroma subsampling introduces small rounding errors, so a tolerance of ±30 per
channel is applied.

The fixtures (three containers + Selenium driver) are defined in conftest.py and are
all session-scoped, meaning they start once for the entire pytest run.
"""

import time
import pytest

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


# ── Tunables ──────────────────────────────────────────────────────────────────

# Maximum seconds to wait for the video element to start playing.
# Covers ICE negotiation (~2-5 s on a Docker bridge) plus VP8 first keyframe
# (~3-5 s in software mode at 640×480@15fps).
VIDEO_PLAY_TIMEOUT = 60

# Pixel colour thresholds for xsetroot -solid red → VP8 encode → browser decode.
RED_MIN = 170       # red channel must be high
GREEN_MAX = 60      # green channel must be low
BLUE_MAX = 60       # blue channel must be low


# ── Helpers ───────────────────────────────────────────────────────────────────

_WAIT_FOR_PLAYING_JS = """
var v = document.getElementById('stream');
if (!v) return {error: 'element #stream not found'};
return {
    readyState:  v.readyState,
    currentTime: v.currentTime,
    paused:      v.paused,
    videoWidth:  v.videoWidth,
    videoHeight: v.videoHeight,
    mediaError:  v.error ? v.error.code : null
};
"""

_SAMPLE_CENTER_PIXEL_JS = """
var video = document.getElementById('stream');
var w = video.videoWidth  || 640;
var h = video.videoHeight || 480;
var canvas = document.createElement('canvas');
canvas.width  = w;
canvas.height = h;
var ctx = canvas.getContext('2d');
ctx.drawImage(video, 0, 0, w, h);
var cx = Math.floor(w / 2);
var cy = Math.floor(h / 2);
var px = ctx.getImageData(cx, cy, 1, 1).data;
return [px[0], px[1], px[2]];
"""


def _wait_for_video_playing(driver, timeout=VIDEO_PLAY_TIMEOUT):
    """
    Poll the <video id="stream"> element until it is actively playing.

    Returns the last recorded state dict.
    Raises pytest.fail immediately if the video element reports a MediaError.
    Raises AssertionError on timeout.
    """
    deadline = time.monotonic() + timeout
    last_state = {}

    while time.monotonic() < deadline:
        last_state = driver.execute_script(_WAIT_FOR_PLAYING_JS)

        if last_state.get("mediaError") is not None:
            pytest.fail(
                f"Video element reported MediaError code {last_state['mediaError']}. "
                "This usually means the codec is unsupported or the stream failed."
            )

        if (
            last_state.get("readyState", 0) >= 3       # HAVE_FUTURE_DATA
            and last_state.get("currentTime", 0) > 0.5  # at least half a second played
            and not last_state.get("paused", True)
        ):
            return last_state

        time.sleep(1)

    pytest.fail(
        f"Video did not start playing within {timeout}s. Last state: {last_state}"
    )


# ── Test ──────────────────────────────────────────────────────────────────────

@pytest.mark.timeout(120)
def test_webrtc_stream_plays_and_shows_red_frame(driver):
    """
    Full end-to-end assertion:

    (a) The <video> element on the streamer web page starts playing a live
        WebRTC stream within VIDEO_PLAY_TIMEOUT seconds.

    (b) The center pixel of the decoded video frame is approximately red,
        matching the solid-red Xvfb root window captured by GStreamer.

    Why ?signalling= is required
    ────────────────────────────
    index.html builds the signalling WebSocket URL from window.location.hostname
    (defaulting to the page server's hostname).  The Selenium driver opens the
    page via the Docker-host-exposed port, so window.location.hostname would be
    the host IP, not "gstreamer".  The ?signalling= query parameter bypasses
    this and points directly at the GStreamer container's Docker DNS alias,
    which is resolvable because Chrome runs inside the same Docker network.
    """
    page_url = "http://gstreamer:8080/?signalling=ws://gstreamer:8443"
    driver.get(page_url)

    # Confirm the video element is in the DOM before polling its state.
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located((By.ID, "stream"))
    )

    # Wait for the stream to start playing.
    _wait_for_video_playing(driver)

    # Capture the center pixel by drawing the current video frame onto a canvas.
    # WebRTC srcObject streams are never cross-origin, so getImageData() does not
    # raise a SecurityError (the canvas is not tainted).
    pixel_rgb = driver.execute_script(_SAMPLE_CENTER_PIXEL_JS)
    r, g, b = pixel_rgb

    assert r > RED_MIN, (
        f"Center pixel red channel too low: R={r} G={g} B={b}. "
        f"Expected a red frame (xsetroot -solid red on Xvfb). "
        f"The pipeline may be capturing a different display or producing black frames."
    )
    assert g < GREEN_MAX, (
        f"Center pixel green channel too high: R={r} G={g} B={b}."
    )
    assert b < BLUE_MAX, (
        f"Center pixel blue channel too high: R={r} G={g} B={b}."
    )
