"""
Integration tests for the streamer-test two-container stack
(desktop-caster + desktop-stream-service).

Level 1 (TestServiceAvailability): verifies the service container's HTTP
  and WebSocket endpoints are reachable and return expected content. No
  browser required; fast.

Level 2 (TestWebRTCStream): drives a headless Chrome browser to load the
  service container's streaming page and confirms a WebRTC video stream
  actually plays — exercises the full pipeline (caster → SRT → service →
  WebRTC → browser).
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

    def test_webrtc_video_plays(self, streaming_container, _caster, _service,
                                browser, turn_params):
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
                        pc.addEventListener('track', function(e) {
                            var ns = e.streams ? e.streams.length : 0;
                            console.log('[diag] track event: kind=' + e.track.kind +
                                        ' streams=' + ns +
                                        ' muted=' + e.track.muted);
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
            service_out, service_err = _service.get_logs()
            caster_out,  caster_err  = _caster.get_logs()
            console_text = "\n".join(
                f"    [{e['level']}] {e['message']}" for e in console_logs
            ) or "    (no browser console output)"
            pytest.fail(
                f"WebRTC video did not start playing within 60 s.\n"
                f"  video state    : {video_state}\n"
                f"  page status    : {status_text!r}\n"
                f"  browser console:\n{console_text}\n"
                f"===== caster stdout =====\n{caster_out.decode(errors='replace')}\n"
                f"===== caster stderr =====\n{caster_err.decode(errors='replace')}\n"
                f"===== service stdout =====\n{service_out.decode(errors='replace')}\n"
                f"===== service stderr =====\n{service_err.decode(errors='replace')}"
            )

        # Sample decoded pixels from the <video> to prove the stream carries
        # the red Xvfb root, not a black/empty placeholder.  Chroma subsampling
        # and YUV<->RGB rounding in the codec shift pure red a few units, so
        # thresholds allow ~20 % slack rather than requiring exactly (255,0,0).
        #
        # Poll with a wait loop because video.currentTime can advance on the
        # first RTP packet before readyState reaches HAVE_CURRENT_DATA (2),
        # and drawImage() on a video without a current frame leaves the canvas
        # at its initial transparent-black state (returning avg RGB = 0,0,0
        # with no exception).  The loop waits for readyState >= 2 and for the
        # drawn canvas to contain non-zero pixels.
        capture_script = """
            const v = document.querySelector('video');
            if (!v || !v.videoWidth || !v.videoHeight) {
                return {stage: 'no-video-size',
                        readyState: v ? v.readyState : -1};
            }
            if (v.readyState < 2) {
                return {stage: 'not-ready',
                        readyState: v.readyState,
                        currentTime: v.currentTime};
            }
            const c = document.createElement('canvas');
            c.width = v.videoWidth;
            c.height = v.videoHeight;
            const ctx = c.getContext('2d');
            try {
                ctx.drawImage(v, 0, 0, c.width, c.height);
            } catch (e) {
                return {stage: 'drawImage-error', error: String(e),
                        readyState: v.readyState};
            }
            let data;
            try {
                data = ctx.getImageData(0, 0, c.width, c.height).data;
            } catch (e) {
                return {stage: 'getImageData-error', error: String(e)};
            }
            // Sample the first pixel and a center pixel separately so we can
            // distinguish "drawImage didn't run" (canvas alpha = 0 everywhere)
            // from "stream is genuinely solid black" (alpha = 255) in the
            // failure message — the root-cause shape is very different.
            const centerIdx = (Math.floor(c.height / 2) * c.width +
                               Math.floor(c.width / 2)) * 4;
            let r = 0, g = 0, b = 0, a = 0;
            const n = data.length / 4;
            for (let i = 0; i < data.length; i += 4) {
                r += data[i]; g += data[i + 1];
                b += data[i + 2]; a += data[i + 3];
            }
            return {
                stage: 'ok',
                width: c.width, height: c.height,
                avgR: r / n, avgG: g / n, avgB: b / n, avgA: a / n,
                firstPixel:  [data[0], data[1], data[2], data[3]],
                centerPixel: [data[centerIdx], data[centerIdx + 1],
                              data[centerIdx + 2], data[centerIdx + 3]],
                readyState: v.readyState,
                currentTime: v.currentTime,
            };
        """

        def frame_is_red(stats):
            return (
                stats is not None
                and stats.get("stage") == "ok"
                and stats.get("avgR", 0) > 200
                and stats.get("avgG", 255) < 60
                and stats.get("avgB", 255) < 60
            )

        last_stats = {}

        def red_frame_available(driver):
            stats = driver.execute_script(capture_script)
            last_stats.clear()
            last_stats.update(stats or {})
            return stats if frame_is_red(stats) else False

        try:
            WebDriverWait(browser, timeout=30, poll_frequency=0.5).until(
                red_frame_available
            )
        except Exception:
            pytest.fail(
                "Decoded WebRTC frame is not red.  Expected R>200, G<60, B<60 "
                f"from the red Xvfb root. Last sample: {last_stats}"
            )
