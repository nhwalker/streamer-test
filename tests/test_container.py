"""
Integration tests for the streamer-test container.

Level 1 (TestServiceAvailability): verifies HTTP and WebSocket services
  are reachable and return expected content. No browser required; fast.

Level 2 (TestWebRTCStream): drives a headless Chrome browser to load the
  streaming page and confirms that a WebRTC video stream actually plays.
"""
import asyncio

import pytest
import requests
import websockets

from selenium.webdriver.support.ui import WebDriverWait


class TestServiceAvailability:
    """HTTP and WebSocket smoke tests — no browser needed."""

    def test_http_returns_200(self, streaming_container):
        http_port, _ = streaming_container
        r = requests.get(f"http://localhost:{http_port}/", timeout=10)
        assert r.status_code == 200

    def test_html_has_video_element(self, streaming_container):
        http_port, _ = streaming_container
        r = requests.get(f"http://localhost:{http_port}/", timeout=10)
        assert "<video" in r.text, "index.html must contain a <video> element"

    def test_html_has_gstwebrtc_api_script(self, streaming_container):
        """Guards against the JS bundle being missing from the container image."""
        http_port, _ = streaming_container
        r = requests.get(f"http://localhost:{http_port}/", timeout=10)
        assert "gstwebrtc-api" in r.text, "index.html must reference gstwebrtc-api"

    def test_gstwebrtc_api_js_served(self, streaming_container):
        """Confirms the JS bundle was copied from the builder stage."""
        http_port, _ = streaming_container
        r = requests.get(
            f"http://localhost:{http_port}/gstwebrtc-api/gstwebrtc-api.js",
            timeout=10,
        )
        assert r.status_code == 200
        assert len(r.content) > 0, "gstwebrtc-api.js must not be empty"

    def test_websocket_accepts_connection(self, streaming_container):
        """
        Signalling server accepts a plain WebSocket upgrade on port 8443.

        Uses asyncio.run() to drive the async websockets API from a
        synchronous test, avoiding a pytest-asyncio dependency.
        """
        _, ws_port = streaming_container

        async def _connect():
            uri = f"ws://localhost:{ws_port}"
            async with websockets.connect(uri, open_timeout=10) as ws:
                await asyncio.sleep(0.2)
                assert not ws.closed, "WebSocket closed immediately after connect"

        asyncio.run(_connect())


class TestWebRTCStream:
    """Browser-driven test that verifies live video playback over WebRTC."""

    def test_webrtc_video_plays(self, streaming_container, browser):
        """
        Headless Chrome loads the streaming page and receives a WebRTC stream.

        video.currentTime > 0 proves the decoder is advancing — encoded
        frames have arrived from the GStreamer pipeline and been decoded.
        Timeout is 30 s to accommodate ICE gathering and codec negotiation.
        """
        http_port, _ = streaming_container
        browser.get(f"http://localhost:{http_port}/")

        def video_is_playing(driver):
            t = driver.execute_script(
                "const v = document.querySelector('video');"
                "return v ? v.currentTime : -1;"
            )
            return isinstance(t, (int, float)) and t > 0

        try:
            WebDriverWait(browser, timeout=30, poll_frequency=0.5).until(
                video_is_playing
            )
        except Exception:
            video_state = browser.execute_script("""
                const v = document.querySelector('video');
                if (!v) return {error: 'no video element'};
                return {
                    currentTime: v.currentTime,
                    readyState:  v.readyState,
                    paused:      v.paused,
                    error:       v.error ? v.error.message : null,
                    srcObject:   v.srcObject ? 'set' : 'null',
                };
            """)
            status_text = browser.execute_script(
                "const s = document.getElementById('status');"
                "return s ? s.textContent : 'status element not found';"
            )
            pytest.fail(
                f"WebRTC video did not start playing within 30 s.\n"
                f"  video state : {video_state}\n"
                f"  page status : {status_text!r}"
            )
