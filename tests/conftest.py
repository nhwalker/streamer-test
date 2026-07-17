"""
pytest fixtures for the streamer-test container integration suite.

Fixture dependency graph:

    xvfb_display (session)              archive_dir, archive_live_dir (session)
         │                                                 │
         ▼                                                 │
    _service (session)  ── runs service container;        │
         │                  ffmpeg x11grab captures xvfb_display,
         │                  mounts archive_dir as /archive,
         │                  mounts archive_live_dir as /archive-live
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

SERVICE_IMAGE = os.environ.get("SERVICE_IMAGE", "desktop-stream-service:ci")
HUB_IMAGE     = os.environ.get("HUB_IMAGE",     "desktop-stream-hub:ci")

XVFB_DISPLAY  = ":99"
XVFB_GEOMETRY = "1280x720x24"
STREAM_WIDTH  = 1280
STREAM_HEIGHT = 720
CROP_HEIGHT   = STREAM_HEIGHT // 2   # 360 — split point for top/bottom tests

# Regular chain uses the image defaults: MediaMTX WHEP on 8889 (UDP 8189),
# RTSP loopback ingest on 8554.
WEBRTC_PORT             = 8889
HTTP_PORT               = 8080

# Two-tone chain (separate Xvfb/service for crop-offset colour tests).
# Ports are kept well apart from the regular chain to allow both to coexist in
# the same pytest session on a host-networked Docker setup.
HUB_HTTP_PORT = 8091

# DESKTOP_SPLITS that produce a top/bottom pair of CROP_HEIGHT-tall regions.
TOP_BOTTOM_SPLITS = (
    f"{STREAM_WIDTH}x{CROP_HEIGHT}+0+0;"
    f"{STREAM_WIDTH}x{CROP_HEIGHT}+0+{CROP_HEIGHT}"
)

# Test data written to a temp file and mounted as /etc/hub/web/streams.json.
HUB_TEST_STREAMS = [
    {"name": "Desktop A", "url": "http://10.0.0.1:8080"},
    {"name": "Desktop B", "url": "http://10.0.0.2:8080"},
]

TWO_TONE_DISPLAY                  = ":98"
TWO_TONE_HTTP_PORT                = 8090
TWO_TONE_WEBRTC_PORT              = 9889
TWO_TONE_WEBRTC_UDP_PORT          = 9189
TWO_TONE_RTSP_PORT                = 9554

# Optional TURN config for the browser side (MediaMTX serves host
# candidates on loopback, so the server side needs no relay).
WEBRTC_TURN_SERVER = os.environ.get("WEBRTC_TURN_SERVER", "")
WEBRTC_TURN_USER   = os.environ.get("WEBRTC_TURN_USER", "")
WEBRTC_TURN_CRED   = os.environ.get("WEBRTC_TURN_CRED", "")


# ── Xvfb + red window ────────────────────────────────────────────────────────

def _sample_pixel(display, x, y):
    """Return the ImageMagick pixel string at (x, y) on *display*."""
    xwd = subprocess.run(
        ["xwd", "-display", display, "-root", "-silent"],
        check=True, timeout=5,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    result = subprocess.run(
        ["convert", "xwd:-", "-format", f"%[pixel:p{{{x},{y}}}]", "info:"],
        check=True, timeout=5, input=xwd.stdout,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return result.stdout.decode(errors="replace").strip()


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
    """Host directory mounted into the service container as /archive.

    Holds completed (timestamp-named) segments only.
    """
    path = tmp_path_factory.mktemp("archive")
    os.chmod(path, 0o777)
    return str(path)


@pytest.fixture(scope="session")
def archive_live_dir(tmp_path_factory):
    """Host directory mounted into the service container as /archive-live.

    Holds the in-progress segment that ffmpeg is currently writing.
    """
    path = tmp_path_factory.mktemp("archive_live")
    os.chmod(path, 0o777)
    return str(path)


# ── Service ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def _service(xvfb_display, archive_dir, archive_live_dir):
    """
    The service container: ffmpeg x11grab captures the Xvfb display →
    live RTSP tiers into MediaMTX (WHEP egress) + archive segments.
    """
    container = (
        DockerContainer(SERVICE_IMAGE)
        .with_env("DISPLAY", xvfb_display)
        .with_env("DESKTOP_NAME", "stream")
        .with_env("STREAM_WIDTH", str(STREAM_WIDTH))
        .with_env("STREAM_HEIGHT", str(STREAM_HEIGHT))
        .with_env("DESKTOP_SPLITS", TOP_BOTTOM_SPLITS)
        .with_env("ARCHIVE_SEGMENT_SEC", "20")
        # Host networking: make sure loopback is among the advertised ICE
        # candidates so the CI browser can connect over 127.0.0.1.
        .with_env("WEBRTC_ADDITIONAL_HOSTS", "127.0.0.1")
        .with_volume_mapping("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")
        .with_volume_mapping(archive_dir, "/archive", "rw")
        .with_volume_mapping(archive_live_dir, "/archive-live", "rw")
        .with_kwargs(network_mode="host", ipc_mode="host")
    )
    with container:
        yield container


# ── streaming_container ───────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def streaming_container(_service):
    """
    Wait until the service's HTTP + WHEP endpoints are reachable,
    then yield (http_port, webrtc_port).
    """
    http_port   = HTTP_PORT
    webrtc_port = WEBRTC_PORT

    # MediaMTX comes up before the web server in entrypoint.sh.
    wait_for_logs(_service, "MediaMTX ready", timeout=60)
    wait_for_logs(_service, "web server on port", timeout=10)

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

    yield http_port, webrtc_port


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


def _wait_for_first_segment(live_dir, timeout=30.0):
    """Wait for the first in-progress stream-NNNNN.mp4 to appear in live_dir."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.isdir(live_dir):
            first = min(
                (f for f in os.listdir(live_dir)
                 if f.startswith("stream-") and f.endswith(".mp4")),
                default=None,
            )
            if first:
                return os.path.join(live_dir, first)
        time.sleep(0.5)
    return None


@pytest.fixture(scope="session")
def first_segment(streaming_container, archive_live_dir, archive_dir, _service):
    """Path to the first in-progress .mp4 segment, waiting up to 30 s.

    The active segment is written into archive_live_dir as a fragmented
    MP4; only after a fragment rotates does it move into archive_dir
    under its timestamp name.
    """
    path = _wait_for_first_segment(archive_live_dir, timeout=30.0)
    if path is None:
        service_out, service_err = _service.get_logs()
        live_listing = os.listdir(archive_live_dir) \
            if os.path.isdir(archive_live_dir) else []
        archive_listing = os.listdir(archive_dir) \
            if os.path.isdir(archive_dir) else []
        raise RuntimeError(
            f"No stream-*.mp4 appeared in {archive_live_dir} within 30 s.\n"
            f"Live dir listing:    {live_listing}\n"
            f"Archive dir listing: {archive_listing}\n"
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


# ── Two-tone Xvfb chain (crop-offset colour tests) ───────────────────────────
#
# The regular Xvfb is entirely red, which cannot distinguish top from bottom.
# This chain runs a separate Xvfb whose top half is red and bottom half is
# blue, so the /top and /bottom WebRTC streams produce different colours.
# If the bottom crop offset were wrong the stream would show red instead of
# blue and the assertion would fail.

@pytest.fixture(scope="session")
def xvfb_two_tone():
    """
    Xvfb on TWO_TONE_DISPLAY with top half red, bottom half blue.

    Two xlogo windows continuously repaint each half — needed because Xvfb
    has no backing store by default. Red covers the top CROP_HEIGHT rows;
    blue covers the bottom CROP_HEIGHT rows.
    """
    xvfb_proc = subprocess.Popen(
        ["Xvfb", TWO_TONE_DISPLAY, "-screen", "0", XVFB_GEOMETRY],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    socket_path = f"/tmp/.X11-unix/X{TWO_TONE_DISPLAY.lstrip(':')}"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if os.path.exists(socket_path):
            break
        time.sleep(0.1)
    else:
        xvfb_proc.terminate()
        raise RuntimeError(f"Two-tone Xvfb socket {socket_path} did not appear within 5 s")

    # Cover the top half with a continuously-redrawn red xlogo window.
    top_geometry = f"{STREAM_WIDTH}x{CROP_HEIGHT}+0+0"
    red_top = subprocess.Popen(
        [
            "xlogo",
            "-display", TWO_TONE_DISPLAY,
            "-geometry", top_geometry,
            "-bg", "#ff0000",
            "-fg", "#ff0000",
            "-bw", "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Cover the bottom half with a continuously-redrawn blue xlogo window.
    bot_geometry = f"{STREAM_WIDTH}x{CROP_HEIGHT}+0+{CROP_HEIGHT}"
    blue_bottom = subprocess.Popen(
        [
            "xlogo",
            "-display", TWO_TONE_DISPLAY,
            "-geometry", bot_geometry,
            "-bg", "#0000ff",
            "-fg", "#0000ff",
            "-bw", "0",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    time.sleep(1.0)
    for name, proc in (("red_top", red_top), ("blue_bottom", blue_bottom)):
        if proc.poll() is not None:
            _, stderr = proc.communicate(timeout=2)
            _cleanup_procs(red_top, blue_bottom, xvfb_proc)
            raise RuntimeError(
                f"Two-tone xlogo ({name}) exited early (rc={proc.returncode}). "
                f"stderr: {stderr.decode(errors='replace')!r}"
            )

    # Verify both halves have the expected colours.
    cx = STREAM_WIDTH // 2
    try:
        top_pixel = _sample_pixel(TWO_TONE_DISPLAY, cx, CROP_HEIGHT // 2)
        bot_pixel = _sample_pixel(TWO_TONE_DISPLAY, cx, CROP_HEIGHT + CROP_HEIGHT // 2)
    except Exception as exc:
        _cleanup_procs(red_top, blue_bottom, xvfb_proc)
        raise RuntimeError(f"Two-tone Xvfb pixel sampling failed: {exc}") from exc

    if "255,0,0" not in top_pixel and top_pixel.lower() not in {"red", "#ff0000"}:
        _cleanup_procs(red_top, blue_bottom, xvfb_proc)
        raise RuntimeError(
            f"Two-tone Xvfb top half is not red at ({cx},{CROP_HEIGHT // 2}); got {top_pixel!r}"
        )
    if "0,0,255" not in bot_pixel and bot_pixel.lower() not in {"blue", "#0000ff"}:
        _cleanup_procs(red_top, blue_bottom, xvfb_proc)
        raise RuntimeError(
            f"Two-tone Xvfb bottom half is not blue at ({cx},{CROP_HEIGHT + CROP_HEIGHT // 2}); "
            f"got {bot_pixel!r}"
        )

    yield TWO_TONE_DISPLAY

    _cleanup_procs(red_top, blue_bottom, xvfb_proc)


@pytest.fixture(scope="session")
def _service_two_tone(xvfb_two_tone):
    """Service container capturing the two-tone Xvfb display directly.

    All listening ports are shifted so this chain can coexist with the
    regular chain on a host-networked Docker setup.
    """
    container = (
        DockerContainer(SERVICE_IMAGE)
        .with_env("DISPLAY", xvfb_two_tone)
        .with_env("DESKTOP_NAME", "stream")
        .with_env("STREAM_WIDTH", str(STREAM_WIDTH))
        .with_env("STREAM_HEIGHT", str(STREAM_HEIGHT))
        .with_env("DESKTOP_SPLITS", TOP_BOTTOM_SPLITS)
        .with_env("WEB_PORT", str(TWO_TONE_HTTP_PORT))
        .with_env("WEBRTC_PORT", str(TWO_TONE_WEBRTC_PORT))
        .with_env("WEBRTC_UDP_PORT", str(TWO_TONE_WEBRTC_UDP_PORT))
        .with_env("MEDIAMTX_RTSP_PORT", str(TWO_TONE_RTSP_PORT))
        .with_env("WEBRTC_ADDITIONAL_HOSTS", "127.0.0.1")
        .with_env("ARCHIVE_SEGMENT_SEC", "20")
        .with_volume_mapping("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")
        .with_kwargs(network_mode="host", ipc_mode="host")
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def streaming_container_two_tone(_service_two_tone):
    """
    Wait for the two-tone service's HTTP + WHEP endpoints, then yield
    (http_port, webrtc_port).
    """
    http_port   = TWO_TONE_HTTP_PORT
    webrtc_port = TWO_TONE_WEBRTC_PORT

    wait_for_logs(_service_two_tone, "MediaMTX ready", timeout=60)
    wait_for_logs(_service_two_tone, "web server on port", timeout=10)

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
        stdout, stderr = _service_two_tone.get_logs()
        raise RuntimeError(
            f"Two-tone HTTP server on :{http_port} did not respond within 10 s.\n"
            f"Container stdout:\n{stdout.decode()}\n"
            f"Container stderr:\n{stderr.decode()}"
        )

    yield http_port, webrtc_port


# ── Hub container ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def hub_streams_file(tmp_path_factory):
    """Write HUB_TEST_STREAMS to a temp file for mounting into the hub container."""
    import json as _json
    p = tmp_path_factory.mktemp("hub") / "streams.json"
    p.write_text(_json.dumps(HUB_TEST_STREAMS))
    return str(p)


@pytest.fixture(scope="session")
def _hub(hub_streams_file):
    """Hub container with the test streams config mounted into its web root."""
    container = (
        DockerContainer(HUB_IMAGE)
        .with_env("WEB_PORT", str(HUB_HTTP_PORT))
        .with_volume_mapping(hub_streams_file, "/etc/hub/web/streams.json", "ro")
        .with_kwargs(network_mode="host")
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def hub_container(_hub):
    """Wait for the hub's HTTP server to be ready, then yield the HTTP port."""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"http://localhost:{HUB_HTTP_PORT}/", timeout=2)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(0.25)
    else:
        stdout, stderr = _hub.get_logs()
        raise RuntimeError(
            f"Hub HTTP server on :{HUB_HTTP_PORT} did not respond within 15 s.\n"
            f"Container stdout:\n{stdout.decode()}\n"
            f"Container stderr:\n{stderr.decode()}"
        )
    yield HUB_HTTP_PORT
