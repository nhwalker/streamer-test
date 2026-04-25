"""
pytest fixtures for the streamer-test container integration suite.

Fixture dependency graph:

    xvfb_display (session)                          archive_dir (session)
         │                                                 │
         ▼                                                 │
    _caster (session)  ── runs caster container            │
         │  RTP push to service on :RTP_PORT against Xvfb  │
         ▼                                                 ▼
    _service (session)  ── runs service container; receives RTP,
         │                  mounts archive_dir as /archive
         ▼
    streaming_container (session)  ──► yields (http_port, ws_port)
         │
         ▼  (tests request streaming_container and browser)
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

# Two images now (caster + service).  CI builds each separately and tags
# them as below; local runs can override via env.
CASTER_IMAGE  = os.environ.get("CASTER_IMAGE",  "desktop-caster:ci")
SERVICE_IMAGE = os.environ.get("SERVICE_IMAGE", "desktop-stream-service:ci")

XVFB_DISPLAY = ":99"
XVFB_GEOMETRY = "1280x720x24"

# Fixed ports for host networking.  RTP/RTCP are UDP, HTTP/WS are TCP.
RTP_PORT  = 5000
HTTP_PORT = 8080
WS_PORT   = 8443

# Optional TURN config for both sides (set in CI to bypass Azure hairpin UDP).
GST_TURN_SERVER    = os.environ.get("GST_WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_SERVER = os.environ.get("WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_USER   = os.environ.get("WEBRTC_TURN_USER", "")
WEBRTC_TURN_CRED   = os.environ.get("WEBRTC_TURN_CRED", "")


# ── Xvfb + red window (unchanged from single-container era) ─────────────────
#
# xlogo from x11-apps is a tiny long-running X11 client that actively redraws
# itself on every Expose event.  Using -bg and -fg both set to the same hex
# red paints the entire window red (the logo glyph is invisible against a
# same-colored background).  We use xlogo rather than "xsetroot -solid" or a
# Tk window because Xvfb does not enable backing store by default — any
# client that paints once and then sits idle has its pixels fall out of the
# framebuffer, leaving ximagesrc to capture black.  xlogo's continuous
# redraw keeps the pixels live.


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
    """Start Xvfb + red xlogo before any test runs.  See module docstring."""
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
    # Relax perms so the service container (running as root inside) can
    # write to the host-created directory regardless of the tmp umask.
    os.chmod(path, 0o777)
    return str(path)


# ── Caster ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def _caster(xvfb_display):
    """
    The caster container: X11 capture → H.264 → RTP push to SERVICE_HOST:RTP_PORT.

    Runs with host networking so the service container (also host-networked)
    can receive RTP at 127.0.0.1, and with host IPC so ximagesrc's MIT-SHM
    attach can reach the host Xvfb's SysV shared-memory segment (otherwise
    ximagesrc captures all-zero frames despite a successful X connection).
    """
    container = (
        DockerContainer(CASTER_IMAGE)
        .with_env("DISPLAY", xvfb_display)
        .with_env("STREAM_WIDTH", "1280")
        .with_env("STREAM_HEIGHT", "720")
        .with_env("SERVICE_HOST", "127.0.0.1")
        .with_env("RTP_PORT", str(RTP_PORT))
        .with_volume_mapping("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")
        .with_kwargs(network_mode="host", ipc_mode="host")
    )
    with container:
        # The caster logs "Starting capture:" once the pipeline is parsed.
        # Give GStreamer a couple of seconds to reach PLAYING so that RTP
        # datagrams are flowing before the service starts.
        wait_for_logs(container, "Starting capture", timeout=30)
        time.sleep(2.0)
        yield container


# ── Service ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def _service(_caster, archive_dir):
    """
    The service container: RTP → tee → Matroska archive + webrtcsink.

    Depends on _caster so the caster is already pushing RTP before the
    service pipeline starts listening on the UDP port.
    """
    container = (
        DockerContainer(SERVICE_IMAGE)
        .with_env("DESKTOP_HOST", "127.0.0.1")   # RTCP RR feedback destination
        .with_env("RTP_PORT", str(RTP_PORT))
        # 20-second segments so test runs produce at least one complete
        # segment well inside the test timeout budget; production default
        # is 600 s.  Keep it an integer so splitmuxsink math is clean.
        .with_env("ARCHIVE_SEGMENT_SEC", "20")
        .with_env("GST_WEBRTC_TURN_SERVER", GST_TURN_SERVER)
        .with_volume_mapping(archive_dir, "/archive", "rw")
        .with_kwargs(network_mode="host")
    )
    with container:
        yield container


# ── streaming_container (compat shim, same return shape as before) ─────────
@pytest.fixture(scope="session")
def streaming_container(_service):
    """
    Wait until the service container's HTTP + WebSocket are reachable,
    then yield (http_port, ws_port).

    With host networking the container's ports are the host's ports
    directly — no dynamic mapping needed.
    """
    http_port = HTTP_PORT
    ws_port = WS_PORT

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
