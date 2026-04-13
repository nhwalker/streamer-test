"""
conftest.py — pytest session fixtures for the WebRTC integration test.

Container startup order (each fixture depends on the previous so pytest
enforces the correct sequence):

  1. docker_network   — shared Docker bridge; all three containers join it.
  2. x11_volume       — named Docker volume for /tmp/.X11-unix socket sharing.
  3. xvfb_image       — UBI-9 Xvfb image built from tests/Dockerfile.xvfb.
  4. gstreamer_image  — streamer image built from the project root Dockerfile.
  5. xvfb_container   — Xvfb on display :1, root window painted solid red.
                        Readiness: xdpyinfo -display :1 exits 0 inside container.
  6. gstreamer_container — container under test; mounts X11 socket volume (ro).
                           Readiness: "Desktop Stream ready" in logs + HTTP 200 on 8080.
  7. selenium_container  — selenium/standalone-chrome; on the same network.
                           Readiness: /wd/hub/status JSON reports ready.
  8. driver           — webdriver.Remote pointed at the Chrome container.
"""

import time
import pathlib

import docker as docker_sdk
import pytest
import requests
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions

from testcontainers.core.container import DockerContainer
from testcontainers.core.image import DockerImage
from testcontainers.core.network import Network

# ── Paths ──────────────────────────────────────────────────────────────────────
TESTS_DIR = pathlib.Path(__file__).parent.resolve()
PROJECT_ROOT = TESTS_DIR.parent.resolve()

# ── Image tags ─────────────────────────────────────────────────────────────────
XVFB_IMAGE_TAG = "webrtc-test-xvfb:latest"
GSTREAMER_IMAGE_TAG = "webrtc-test-gstreamer:latest"

# ── Named volume for X11 socket sharing ───────────────────────────────────────
X11_VOLUME_NAME = "webrtc-test-x11-socket"


# ─── Wait helpers ─────────────────────────────────────────────────────────────

def _wait_exec(container, cmd, timeout=30, interval=1):
    """
    Run cmd inside the (already-started) container until its exit code is 0.

    Uses container._container.exec_run() — the underlying docker-py Container
    object populated by testcontainers after start().
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = container._container.exec_run(cmd)
            if result.exit_code == 0:
                return
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(
        f"Command {cmd!r} did not succeed within {timeout}s in container "
        f"{container._container.short_id}"
    )


def _wait_log(container, text, timeout=60, interval=2):
    """
    Poll container logs (stdout + stderr combined) until `text` appears.
    """
    deadline = time.monotonic() + timeout
    encoded = text.encode()
    while time.monotonic() < deadline:
        try:
            logs = container._container.logs(stdout=True, stderr=True)
            if encoded in logs:
                return
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"Log text {text!r} not found within {timeout}s")


def _wait_http(url, timeout=30, interval=1):
    """
    Poll url with GET until a 2xx response is received.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code < 300:
                return
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"HTTP {url!r} not ready within {timeout}s")


# ─── Session fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def docker_client():
    """Raw docker-py client; used for named volume lifecycle management."""
    client = docker_sdk.from_env()
    yield client
    client.close()


@pytest.fixture(scope="session")
def x11_volume(docker_client):
    """
    Named Docker volume for sharing /tmp/.X11-unix between containers.

    The Xvfb container mounts it read-write and writes the X11 socket into it.
    The GStreamer container mounts it read-only; it can still connect() to the
    socket (filesystem read-only prevents file creation, not socket I/O).

    A named volume (no path separator in the name) is recognised by docker-py
    and the Docker daemon as a managed volume rather than a host bind-mount.
    This works identically on a developer workstation and inside a CI runner
    that is itself a container.
    """
    vol = docker_client.volumes.create(name=X11_VOLUME_NAME)
    yield vol
    vol.remove(force=True)


@pytest.fixture(scope="session")
def docker_network():
    """
    Shared Docker bridge network for all three containers.

    The GStreamer container gets alias "gstreamer" so the Chrome browser
    (running inside the Selenium container on the same network) resolves it
    via Docker's embedded DNS rather than relying on window.location.hostname.
    """
    with Network() as network:
        yield network


@pytest.fixture(scope="session")
def xvfb_image():
    """Build the Xvfb container image from tests/Dockerfile.xvfb."""
    with DockerImage(
        path=str(TESTS_DIR),
        dockerfile_path="Dockerfile.xvfb",
        tag=XVFB_IMAGE_TAG,
        clean_up=True,
    ) as image:
        yield str(image)


@pytest.fixture(scope="session")
def gstreamer_image(docker_client):
    """
    Return the GStreamer image tag, building it from source only if it is not
    already present in the local Docker daemon.

    In CI the workflow pre-builds the image with BuildKit and loads it into
    the daemon before pytest runs; the build is skipped here (cache hit).

    On a developer machine the first run compiles Rust (~20-40 min); subsequent
    runs find the image in the daemon and skip the build.

    clean_up=False: never remove a potentially 40-minute build artefact.
    """
    try:
        docker_client.images.get(GSTREAMER_IMAGE_TAG)
        yield GSTREAMER_IMAGE_TAG
    except docker_sdk.errors.ImageNotFound:
        with DockerImage(
            path=str(PROJECT_ROOT),
            dockerfile_path="Dockerfile",
            tag=GSTREAMER_IMAGE_TAG,
            clean_up=False,
        ) as image:
            yield str(image)


@pytest.fixture(scope="session")
def xvfb_container(docker_network, x11_volume, xvfb_image):
    """
    Start the Xvfb container and wait until the X server accepts connections.

    Volume mount (rw): the named X11 volume at /tmp/.X11-unix.  Xvfb creates
    the Unix socket /tmp/.X11-unix/X1 inside the volume; the GStreamer container
    will mount the same volume to reach it.

    Readiness probe: run xdpyinfo -display :1 inside the container until it
    exits 0, confirming the socket exists and accepts connections (not merely
    that the Xvfb process has started).
    """
    container = (
        DockerContainer(xvfb_image)
        .with_network(docker_network)
        .with_network_aliases("xvfb")
        .with_volume_mapping(X11_VOLUME_NAME, "/tmp/.X11-unix", "rw")
    )
    container.start()
    _wait_exec(container, ["xdpyinfo", "-display", ":1"], timeout=30)
    yield container
    container.stop()


@pytest.fixture(scope="session")
def gstreamer_container(docker_network, x11_volume, gstreamer_image, xvfb_container):
    """
    Start the GStreamer streaming container under test.

    Ordering: the xvfb_container fixture argument ensures Xvfb is fully ready
    (xdpyinfo probe passed) before this container starts.  This guarantees the
    X11 socket exists in the shared volume when entrypoint.sh runs its preflight:
        gst-launch-1.0 ximagesrc num-buffers=1 ! fakesink sync=false

    Volume mount (ro): read-only is sufficient for X11 clients — ximagesrc only
    needs to connect() to the socket, not create files in the directory.

    Environment overrides for the test:
      STREAM_CODEC=vp8      VP8 produces keyframes in ~3-5 s vs VP9's ~10-15 s,
                            making the video-playing assertion reliable under the
                            60 s timeout without a hardware encoder.
      STREAM_WIDTH/HEIGHT   Lower resolution reduces CI CPU load.
      STREAM_FRAMERATE=15   Half the default; sufficient for pixel verification.
      STREAM_BITRATE_KBPS   Reduced to match the lower resolution.

    Readiness: wait for the "Desktop Stream ready" banner in container logs
    (printed by entrypoint.sh after signalling server + HTTP server are up),
    then confirm HTTP 200 on the exposed web port from the test runner.
    """
    container = (
        DockerContainer(gstreamer_image)
        .with_network(docker_network)
        .with_network_aliases("gstreamer")
        .with_exposed_ports(8080, 8443)
        .with_volume_mapping(X11_VOLUME_NAME, "/tmp/.X11-unix", "ro")
        .with_env("DISPLAY", ":1")
        .with_env("STREAM_CODEC", "vp8")
        .with_env("STREAM_WIDTH", "640")
        .with_env("STREAM_HEIGHT", "480")
        .with_env("STREAM_FRAMERATE", "15")
        .with_env("STREAM_BITRATE_KBPS", "500")
    )
    container.start()

    # Wait for entrypoint.sh startup banner — confirms all three services
    # (signalling server, HTTP server, GStreamer pipeline) are running.
    _wait_log(container, "Desktop Stream ready", timeout=120)

    # Secondary HTTP check from the test runner's perspective.
    host = container.get_container_host_ip()
    port = container.get_exposed_port(8080)
    _wait_http(f"http://{host}:{port}/", timeout=30)

    yield container
    container.stop()


@pytest.fixture(scope="session")
def selenium_container(docker_network, gstreamer_container):
    """
    Start selenium/standalone-chrome on the shared network.

    The selenium/standalone-chrome image runs Chrome with its own internal
    Xvfb virtual display; no physical monitor is needed.

    Network: same docker_network as the GStreamer container so Docker DNS
    resolves "gstreamer" to the GStreamer container's IP inside Chrome.

    Readiness: poll /wd/hub/status until the JSON payload reports ready=true.
    """
    container = (
        DockerContainer("selenium/standalone-chrome:latest")
        .with_network(docker_network)
        .with_network_aliases("selenium")
        .with_exposed_ports(4444)
    )
    container.start()

    host = container.get_container_host_ip()
    port = container.get_exposed_port(4444)
    _wait_http(f"http://{host}:{port}/wd/hub/status", timeout=60)

    yield container
    container.stop()


@pytest.fixture(scope="session")
def driver(selenium_container):
    """
    Selenium WebDriver (Chrome) connected to the standalone-chrome container.

    Chrome arguments:
      --no-sandbox                       required when Chrome runs inside Docker.
      --disable-dev-shm-usage            /dev/shm is small in Docker; use /tmp.
      --autoplay-policy=no-user-gesture-required
                                         allow <video autoplay> without a click.
      --use-fake-ui-for-media-stream     suppress camera/microphone dialogs
                                         (not strictly needed for receive-only
                                         WebRTC but avoids unexpected pop-ups).
      --disable-gpu                      no GPU available in CI.

    Note: --headless is intentionally omitted.  selenium/standalone-chrome
    provides an internal virtual display and Chrome has better WebRTC
    compatibility in non-headless mode.
    """
    host = selenium_container.get_container_host_ip()
    port = selenium_container.get_exposed_port(4444)
    selenium_url = f"http://{host}:{port}/wd/hub"

    options = ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--disable-gpu")

    d = webdriver.Remote(command_executor=selenium_url, options=options)
    yield d
    d.quit()
