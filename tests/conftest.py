"""
pytest fixtures for the streamer-test container integration suite.

Fixture dependency graph:

    xvfb_display (session)
         │
         ▼
    _container (session)  ──► yields raw DockerContainer
         │
         ▼
    streaming_container (session)  ──► yields (http_port, ws_port)
         │
         ▼  (tests request both streaming_container and browser)
    browser (function)
"""
import os
import subprocess
import time

import pytest
import requests

from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

# Allow CI to override the image name; default matches the docker build step.
TEST_IMAGE = os.environ.get("TEST_IMAGE", "streamer-test:ci")

XVFB_DISPLAY = ":99"
XVFB_GEOMETRY = "1280x720x24"

# Fixed ports used when the container runs with host networking.
HTTP_PORT = 8080
WS_PORT = 8443

# Optional TURN config for both sides (set in CI to bypass Azure hairpin UDP).
# GStreamer uses it via webrtcbin-ready in pipeline.py (format: turn://u:p@h:p).
# Chrome uses it via ?turn_uri URL param (format: turn:h:p with separate u/p).
GST_TURN_SERVER    = os.environ.get("GST_WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_SERVER = os.environ.get("WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_USER   = os.environ.get("WEBRTC_TURN_USER", "")
WEBRTC_TURN_CRED   = os.environ.get("WEBRTC_TURN_CRED", "")


# xlogo from x11-apps is a tiny long-running X11 client that actively redraws
# itself on every Expose event.  Using -bg and -fg both set to the same hex
# red paints the entire window red (the logo glyph is invisible against a
# same-colored background).  We use xlogo rather than "xsetroot -solid" or a
# Tk window because Xvfb does not enable backing store by default — any
# client that paints once and then sits idle has its pixels fall out of the
# framebuffer, leaving ximagesrc to capture black.  xlogo's continuous
# redraw keeps the pixels live.


def _assert_xvfb_is_red(display, xvfb_proc, red_window_proc):
    """
    Confirm the Xvfb root window's center pixel is red before yielding
    control to the container.  Uses xwd (x11-utils) to dump the framebuffer
    and ImageMagick's "convert" to read the sample pixel, because both are
    already available in CI and neither depends on a Python X11 binding.

    Raises RuntimeError with the observed color on mismatch so the failure
    surfaces at fixture-setup time (clearly a host painting problem) rather
    than inside the browser-driven WebRTC test (which is ambiguous).
    """
    width, height = (int(x) for x in XVFB_GEOMETRY.split("x")[:2])
    cx, cy = width // 2, height // 2

    try:
        xwd = subprocess.run(
            ["xwd", "-display", display, "-root", "-silent"],
            check=True, timeout=5,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        _cleanup_procs(red_window_proc, xvfb_proc)
        raise RuntimeError(
            f"xwd failed on {display}: {exc}. "
            "Install x11-utils on the test host."
        ) from exc

    try:
        pixel = subprocess.run(
            ["convert", "xwd:-", "-format", f"%[pixel:p{{{cx},{cy}}}]", "info:"],
            check=True, timeout=5, input=xwd.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        _cleanup_procs(red_window_proc, xvfb_proc)
        raise RuntimeError(
            f"ImageMagick 'convert' failed reading xwd output: {exc}. "
            "Install imagemagick on the test host."
        ) from exc

    sample = pixel.stdout.decode(errors="replace").strip()
    # "convert" emits e.g. "srgb(255,0,0)" or "red" depending on version.
    if "255,0,0" not in sample and sample.lower() not in {"red", "#ff0000"}:
        _cleanup_procs(red_window_proc, xvfb_proc)
        raise RuntimeError(
            f"Xvfb framebuffer is not red at ({cx},{cy}); got {sample!r}. "
            "The red-window helper is not painting the display — "
            "the WebRTC test would fail with an all-black stream."
        )


def _cleanup_procs(*procs):
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass


@pytest.fixture(scope="session")
def xvfb_display():
    """
    Start Xvfb on display :99 before any test runs.

    Polls for the X11 socket before yielding so the container's
    entrypoint X11 pre-flight check (ximagesrc num-buffers=1) cannot
    race ahead of Xvfb being ready.

    Also launches a borderless full-screen xlogo painted #ff0000 on
    the display, so pixels captured by the container's ximagesrc are a
    known color.  The WebRTC test then asserts the decoded frame is red,
    which distinguishes "stream is live" from "stream carries actual
    display content".
    """
    proc = subprocess.Popen(
        ["Xvfb", XVFB_DISPLAY, "-screen", "0", XVFB_GEOMETRY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    socket_path = f"/tmp/.X11-unix/X{XVFB_DISPLAY.lstrip(':')}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError(f"Xvfb socket {socket_path} did not appear within 5 s")

    # XVFB_GEOMETRY is "WxHxD" — strip the depth for the xlogo -geometry arg.
    width_height = "x".join(XVFB_GEOMETRY.split("x")[:2]) + "+0+0"
    red_window = subprocess.Popen(
        [
            "xlogo",
            "-display", XVFB_DISPLAY,
            "-geometry", width_height,
            "-bg", "#ff0000",
            "-fg", "#ff0000",
            "-bw", "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Give xlogo a moment to map and paint before the container starts
    # probing ximagesrc.  If xlogo failed to launch (e.g. x11-apps missing),
    # surface the stderr immediately rather than letting the WebRTC test
    # fail far downstream with an opaque "frame is not red" message.
    time.sleep(1.0)
    if red_window.poll() is not None:
        _, stderr = red_window.communicate(timeout=2)
        proc.terminate()
        raise RuntimeError(
            f"xlogo red-window helper exited early (rc={red_window.returncode}). "
            f"stderr: {stderr.decode(errors='replace')!r}. "
            "Install x11-apps on the test host."
        )

    # Host-side verification: dump the Xvfb framebuffer with xwd and parse a
    # sample pixel with ImageMagick's "convert".  This isolates painting
    # failures on the test host from capture/encode failures in the
    # container — a useful split because the two surface identically at the
    # browser end (all-black frame).
    _assert_xvfb_is_red(XVFB_DISPLAY, proc, red_window)

    yield XVFB_DISPLAY

    red_window.terminate()
    try:
        red_window.wait(timeout=5)
    except subprocess.TimeoutExpired:
        red_window.kill()

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def _container(xvfb_display):
    """
    Raw DockerContainer object, kept alive for the whole test session.

    Runs with host networking so the GStreamer pipeline and headless Chrome
    both use 127.0.0.1 ICE candidates, eliminating Docker-bridge ICE
    connectivity failures in CI.

    Exposed separately from streaming_container so tests that need to
    inspect container logs on failure can request this fixture directly
    without changing the streaming_container API.
    """
    container = (
        DockerContainer(TEST_IMAGE)
        .with_env("DISPLAY", xvfb_display)
        .with_env("STREAM_WIDTH", "1280")
        .with_env("STREAM_HEIGHT", "720")
        # pipeline.py reads this and calls add-turn-server on each webrtcbin.
        .with_env("GST_WEBRTC_TURN_SERVER", GST_TURN_SERVER)
        .with_volume_mapping("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")
        # Host networking so GStreamer can reach coturn on 127.0.0.1.
        # Host IPC namespace so ximagesrc's MIT-SHM requests can attach to
        # SysV shared-memory segments created by the host Xvfb — without
        # this, ximagesrc silently captures all-zero frames even though the
        # X connection itself succeeds.
        .with_kwargs(network_mode="host", ipc_mode="host")
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def streaming_container(_container):
    """
    Wait until HTTP and WebSocket services are reachable, then yield
    (http_port, ws_port).

    With host networking the container's ports are the host's ports
    directly — no dynamic mapping is needed.
    """
    http_port = HTTP_PORT
    ws_port = WS_PORT

    # The web server log line is the last thing entrypoint.sh prints
    # before handing off to pipeline.py, so it confirms both the
    # signalling server and HTTP server are up.
    wait_for_logs(_container, "web server on port", timeout=60)

    # Secondary HTTP poll closes the window between the log line
    # appearing and Python's http.server actually calling listen().
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"http://localhost:{http_port}/", timeout=2)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.25)
    else:
        stdout, stderr = _container.get_logs()
        raise RuntimeError(
            f"HTTP server on :{http_port} did not respond within 10 s.\n"
            f"Container stdout:\n{stdout.decode()}\n"
            f"Container stderr:\n{stderr.decode()}"
        )

    yield http_port, ws_port


@pytest.fixture(scope="session")
def turn_params():
    """
    URL query string fragment to configure a TURN relay for the WebRTC test.

    Returns "&turn_uri=...&turn_user=...&turn_cred=..." when WEBRTC_TURN_SERVER
    is set in the environment (CI only), or "" for local runs without coturn.
    """
    from urllib.parse import urlencode

    if not WEBRTC_TURN_SERVER:
        return ""
    return "&" + urlencode({
        "turn_uri": WEBRTC_TURN_SERVER,
        "turn_user": WEBRTC_TURN_USER,
        "turn_cred": WEBRTC_TURN_CRED,
    })


@pytest.fixture(scope="function")
def browser():
    """
    Headless Chrome WebDriver configured for WebRTC receive.

    --use-fake-ui-for-media-stream  auto-grants media permissions so the
                                    gstwebrtc-api JS can call getUserMedia
                                    without a prompt blocking the test.
    --autoplay-policy=no-user-gesture-required  allows <video> autoplay
                                    without a user click, which headless
                                    Chrome otherwise blocks.
    --disable-features=WebRtcHideLocalIpsWithMdns
                                    expose real IP addresses in ICE
                                    candidates instead of mDNS hostnames,
                                    required for loopback ICE to work.
    goog:loggingPrefs browser=ALL   captures JS console output so failures
                                    include browser-side error messages.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")
    # Allow loopback (127.x.x.x) as ICE host and relay candidates.
    # Required when coturn relays via --relay-ip=127.0.0.1; without this flag
    # Chrome silently drops relay candidates on loopback addresses.
    options.add_argument("--allow-loopback-for-peer-connection")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)

    yield driver

    driver.quit()
