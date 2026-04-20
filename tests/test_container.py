"""
Integration tests for the streamer-test container.

Level 1 (TestServiceAvailability): verifies HTTP and WebSocket services
  are reachable and return expected content. No browser required; fast.

Level 2 (TestWebRTCStream): drives a headless Chrome browser to load the
  streaming page and confirms that a WebRTC video stream actually plays.
"""
import asyncio
import re

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
        page = requests.get(f"http://localhost:{http_port}/", timeout=10)
        # Derive the path from the actual import statement in index.html so
        # the test is not brittle against build-output filename changes.
        m = re.search(r"""(?:from|src=)\s*['"]([^'"]*gstwebrtc-api[^'"]*\.js)['"]""", page.text)
        assert m, "Could not find a gstwebrtc-api JS src/import in index.html"
        js_path = m.group(1).lstrip("./")
        r = requests.get(f"http://localhost:{http_port}/{js_path}", timeout=10)
        if r.status_code != 200:
            listing = requests.get(
                f"http://localhost:{http_port}/gstwebrtc-api/", timeout=10
            )
            found = re.findall(r'href="([^"?#]+)"', listing.text)
            pytest.fail(
                f"JS bundle at /{js_path} returned {r.status_code}.\n"
                f"Files in /gstwebrtc-api/: {found}"
            )
        assert len(r.content) > 0, "gstwebrtc-api JS bundle must not be empty"

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
                # websockets 12 exposed `ws.closed`; 13+ replaced it with
                # `ws.state` (an enum).  Use `state.name` which works in both.
                assert ws.state.name == "OPEN", "WebSocket closed immediately after connect"

        asyncio.run(_connect())


class TestWebRTCStream:
    """Browser-driven test that verifies live video playback over WebRTC."""

    def test_webrtc_video_plays(self, streaming_container, _container, browser, turn_params):
        """
        Headless Chrome loads the streaming page and receives a WebRTC stream.

        video.currentTime > 0 proves the decoder is advancing — encoded
        frames have arrived from the GStreamer pipeline and been decoded.
        Timeout is 60 s to accommodate ICE gathering and codec negotiation.

        turn_params adds TURN relay candidates when WEBRTC_TURN_SERVER is set
        (required in CI on Azure VMs where same-IP UDP hairpin is blocked).
        """
        http_port, ws_port = streaming_container

        # Inject diagnostic hooks that log RTCPeerConnection config and ICE
        # events to the browser console.  Runs on every new document so all
        # ICE candidates (including relay) and the iceServers config used by
        # gstwebrtc-api are visible in the captured browser log on failure.
        browser.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": """
                (function() {
                    var _RPC = window.RTCPeerConnection;
                    function DiagRPC(cfg) {
                        console.log('[diag] RTCPeerConnection config: ' +
                                    JSON.stringify(cfg));
                        var pc = new _RPC(cfg);
                        pc.addEventListener('icecandidate', function(e) {
                            console.log('[diag] ICE candidate: ' +
                                        (e.candidate ? e.candidate.candidate
                                                     : '(end-of-candidates)'));
                        });
                        pc.addEventListener('iceconnectionstatechange', function() {
                            console.log('[diag] ICE state: ' +
                                        pc.iceConnectionState);
                        });
                        pc.addEventListener('connectionstatechange', function() {
                            console.log('[diag] connection state: ' +
                                        pc.connectionState);
                        });
                        return pc;
                    }
                    DiagRPC.prototype = _RPC.prototype;
                    window.RTCPeerConnection = DiagRPC;
                })();
            """},
        )

        # Pass the host-mapped signalling port and, in CI, a loopback TURN relay
        # so ICE relay candidates bypass Azure's same-IP UDP hairpin restriction.
        browser.get(
            f"http://localhost:{http_port}/"
            f"?signalling=ws://localhost:{ws_port}{turn_params}"
        )

        def video_is_playing(driver):
            t = driver.execute_script(
                "const v = document.querySelector('video');"
                "return v ? v.currentTime : -1;"
            )
            return isinstance(t, (int, float)) and t > 0

        try:
            WebDriverWait(browser, timeout=60, poll_frequency=0.5).until(
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
            try:
                console_logs = browser.get_log("browser")
            except Exception:
                console_logs = []
            stdout, stderr = _container.get_logs()
            console_text = "\n".join(
                f"    [{e['level']}] {e['message']}" for e in console_logs
            ) or "    (no browser console output)"
            pytest.fail(
                f"WebRTC video did not start playing within 60 s.\n"
                f"  video state    : {video_state}\n"
                f"  page status    : {status_text!r}\n"
                f"  browser console:\n{console_text}\n"
                f"  container stdout:\n{stdout.decode(errors='replace')}\n"
                f"  container stderr:\n{stderr.decode(errors='replace')}"
            )
