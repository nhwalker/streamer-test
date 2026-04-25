"""
pytest fixtures for the streamer-test container integration suite.

Fixture dependency graph:

    xvfb_display (session)                          archive_dir (session)
         │                                                 │
         ▼                                                 │
    _caster (session)  ── runs caster container            │
         │  webrtcsink published to caster's signalling    │
         ▼  server on CASTER_SIGNALLING_PORT               ▼
    _service (session)  ── runs service container; webrtcsrc dials caster,
         │                  mounts archive_dir as /archive
         ▼
    streaming_container (session)  ──► yields (http_port, ws_port)
         │
         ▼  (tests request streaming_container and browser)
    browser (function)
"""
import os
import re
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

CASTER_IMAGE  = os.environ.get("CASTER_IMAGE",  "desktop-caster:ci")
SERVICE_IMAGE = os.environ.get("SERVICE_IMAGE", "desktop-stream-service:ci")

XVFB_DISPLAY  = ":99"
XVFB_GEOMETRY = "1280x720x24"

# The caster's signalling server port must not collide with the service's
# signalling server (both run on the test host via host networking).
CASTER_SIGNALLING_PORT  = 8445   # caster exposes this; service's webrtcsrc dials it
SERVICE_SIGNALLING_PORT = 8443   # service's browser-facing signalling
HTTP_PORT               = 8080

# Optional TURN config for both sides.
GST_TURN_SERVER    = os.environ.get("GST_WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_SERVER = os.environ.get("WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_USER   = os.environ.get("WEBRTC_TURN_USER", "")
WEBRTC_TURN_CRED   = os.environ.get("WEBRTC_TURN_CRED", "")


# ── Xvfb + red window ────────────────────────────────────────────────────────

def _assert_xvfb_is_red(display, xvfb_proc, red_window_proc):
    """Confirm the Xvfb root window's center pixel is red before yielding."""
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
    """Start Xvfb + red xlogo before any test runs."""
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

    time.sleep(1.0)
    if red_window.poll() is not None:
        _, stderr = red_window.communicate(timeout=2)
        proc.terminate()
        raise RuntimeError(
            f"xlogo red-window helper exited early (rc={red_window.returncode}). "
            f"stderr: {stderr.decode(errors='replace')!r}. "
            "Install x11-apps on the test host."
        )

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
def archive_dir(tmp_path_factory):
    """Host directory mounted into the service container as /archive."""
    path = tmp_path_factory.mktemp("archive")
    os.chmod(path, 0o777)
    return str(path)


# ── Caster ───────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def _caster(xvfb_display):
    """
    The caster container: X11 capture → webrtcsink (publishes to its own
    signalling server on CASTER_SIGNALLING_PORT).

    Must start before the service so the signalling server is ready when
    webrtcsrc on the service tries to connect.
    """
    container = (
        DockerContainer(CASTER_IMAGE)
        .with_env("DISPLAY", xvfb_display)
        .with_env("STREAM_WIDTH", "1280")
        .with_env("STREAM_HEIGHT", "720")
        .with_env("SIGNALLING_PORT", str(CASTER_SIGNALLING_PORT))
        .with_volume_mapping("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")
        .with_kwargs(network_mode="host", ipc_mode="host")
    )
    with container:
        # Wait until the caster's signalling server is confirmed ready.
        wait_for_logs(container, "Signalling server ready", timeout=30)
        time.sleep(1.0)   # give webrtcsink time to register as producer
        yield container


# ── Caster peer ID ───────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def caster_peer_id(_caster):
    """Extract the caster's randomly-assigned signalling peer ID from its logs."""
    wait_for_logs(_caster, "registered as a producer", timeout=15)
    stdout, _ = _caster.get_logs()
    m = re.search(
        r'registered as a producer \[peer_id=([^\]]+)\]',
        stdout.decode(errors='replace'),
    )
    if not m:
        raise RuntimeError(
            "Could not find 'registered as a producer [peer_id=...]' in caster logs"
        )
    peer_id = m.group(1)
    print(f'[conftest] caster peer_id = {peer_id}', flush=True)
    return peer_id


# ── Service ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def _service(_caster, caster_peer_id, archive_dir):
    """
    The service container: webrtcsrc dials caster → tee → archive + webrtcsink.

    Depends on _caster so the caster's signalling server is up before
    webrtcsrc attempts to connect.
    """
    container = (
        DockerContainer(SERVICE_IMAGE)
        .with_env("CASTER_HOST", "127.0.0.1")
        .with_env("CASTER_SIGNALLING_PORT", str(CASTER_SIGNALLING_PORT))
        .with_env("CASTER_PEER_ID", caster_peer_id)
        .with_env("SIGNALLING_PORT", str(SERVICE_SIGNALLING_PORT))
        .with_env("ARCHIVE_SEGMENT_SEC", "20")
        .with_env("GST_WEBRTC_TURN_SERVER", GST_TURN_SERVER)
        .with_volume_mapping(archive_dir, "/archive", "rw")
        .with_kwargs(network_mode="host")
    )
    with container:
        yield container


# ── streaming_container ───────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def streaming_container(_service):
    """
    Wait until the service's HTTP + WebSocket endpoints are reachable,
    then yield (http_port, ws_port).
    """
    http_port = HTTP_PORT
    ws_port   = SERVICE_SIGNALLING_PORT

    wait_for_logs(_service, "web server on port", timeout=60)

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
        stdout, stderr = _service.get_logs()
        raise RuntimeError(
            f"HTTP server on :{http_port} did not respond within 10 s.\n"
            f"Container stdout:\n{stdout.decode()}\n"
            f"Container stderr:\n{stderr.decode()}"
        )

    yield http_port, ws_port


@pytest.fixture(scope="session")
def turn_params():
    """URL query fragment to configure a TURN relay for the browser."""
    from urllib.parse import urlencode

    if not WEBRTC_TURN_SERVER:
        return ""
    return "&" + urlencode({
        "turn_uri": WEBRTC_TURN_SERVER,
        "turn_user": WEBRTC_TURN_USER,
        "turn_cred": WEBRTC_TURN_CRED,
    })


def _wait_for_first_segment(archive_dir, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        first = min(
            (f for f in os.listdir(archive_dir)
             if f.startswith("stream-") and f.endswith(".mkv")),
            default=None,
        )
        if first:
            return os.path.join(archive_dir, first)
        time.sleep(0.5)
    return None


@pytest.fixture(scope="session")
def first_segment(streaming_container, archive_dir, _caster, _service):
    """Path to the first .mkv archive segment, waiting up to 30 s."""
    path = _wait_for_first_segment(archive_dir, timeout=30.0)
    if path is None:
        service_out, service_err = _service.get_logs()
        caster_out, caster_err   = _caster.get_logs()
        listing = os.listdir(archive_dir) if os.path.isdir(archive_dir) else []
        raise RuntimeError(
            f"No stream-*.mkv appeared in {archive_dir} within 30 s.\n"
            f"Directory listing: {listing}\n"
            f"===== caster stdout =====\n{caster_out.decode(errors='replace')}\n"
            f"===== caster stderr =====\n{caster_err.decode(errors='replace')}\n"
            f"===== service stdout =====\n{service_out.decode(errors='replace')}\n"
            f"===== service stderr =====\n{service_err.decode(errors='replace')}"
        )
    return path


@pytest.fixture(scope="function")
def browser():
    """Headless Chrome WebDriver configured for WebRTC receive."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-features=WebRtcHideLocalIpsWithMdns")
    options.add_argument("--allow-loopback-for-peer-connection")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)

    yield driver

    driver.quit()
