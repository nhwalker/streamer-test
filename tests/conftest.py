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


@pytest.fixture(scope="session")
def xvfb_display():
    """
    Start Xvfb on display :99 before any test runs.

    Polls for the X11 socket before yielding so the container's
    entrypoint X11 pre-flight check (ximagesrc num-buffers=1) cannot
    race ahead of Xvfb being ready.
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

    yield XVFB_DISPLAY

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def _container(xvfb_display):
    """
    Raw DockerContainer object, kept alive for the whole test session.

    Exposed separately from streaming_container so tests that need to
    inspect container logs on failure can request this fixture directly
    without changing the streaming_container API.
    """
    container = (
        DockerContainer(TEST_IMAGE)
        .with_env("DISPLAY", xvfb_display)
        .with_volume_mapping("/tmp/.X11-unix", "/tmp/.X11-unix", "rw")
        .with_exposed_ports(8080, 8443)
    )
    with container:
        yield container


@pytest.fixture(scope="session")
def streaming_container(_container):
    """
    Wait until HTTP and WebSocket services are reachable, then yield
    (http_port, ws_port).

    Uses dynamic port mapping (with_exposed_ports) so the fixture works
    in CI environments where 8080/8443 may already be bound.
    """
    # Read port bindings first, while the container is freshly running.
    # testcontainers 4.8+ makes get_exposed_port() call wait_until_ready()
    # internally, which raises TimeoutError if the container has already
    # exited.  Docker assigns host port mappings at creation time, so we
    # can safely read them before the services are ready.
    http_port = int(_container.get_exposed_port(8080))
    ws_port = int(_container.get_exposed_port(8443))

    # The web server log line is the last thing entrypoint.sh prints
    # before handing off to pipeline.sh, so it confirms both the
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
    goog:loggingPrefs browser=ALL   captures JS console output so failures
                                    include browser-side error messages.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--use-fake-ui-for-media-stream")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL"})

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(30)

    yield driver

    driver.quit()
