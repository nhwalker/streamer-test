"""
Integration tests for the desktop-stream-hub container.

Level 1 (TestHubAvailability): verifies the hub's HTTP endpoints are
reachable and return the expected content. No browser required; fast.
The hub container serves a static-file web root; streams.json is mounted
in at runtime and served as a plain file — no custom backend logic.
"""
import requests

from conftest import HUB_TEST_STREAMS


class TestHubAvailability:
    """HTTP smoke tests for the hub container — no browser needed."""

    def test_http_returns_200(self, hub_container):
        r = requests.get(f"http://localhost:{hub_container}/", timeout=10)
        assert r.status_code == 200

    def test_html_has_grid_element(self, hub_container):
        r = requests.get(f"http://localhost:{hub_container}/", timeout=10)
        assert 'id="grid"' in r.text, "index.html must contain an element with id='grid'"

    def test_html_fetches_streams_json(self, hub_container):
        """Guards against the fetch('/streams.json') call being accidentally removed."""
        r = requests.get(f"http://localhost:{hub_container}/", timeout=10)
        assert "streams.json" in r.text, "index.html must reference streams.json"

    def test_streams_json_returns_200(self, hub_container):
        r = requests.get(f"http://localhost:{hub_container}/streams.json", timeout=10)
        assert r.status_code == 200

    def test_streams_json_is_valid_json(self, hub_container):
        r = requests.get(f"http://localhost:{hub_container}/streams.json", timeout=10)
        data = r.json()
        assert isinstance(data, list), "streams.json must be a JSON array"

    def test_streams_json_matches_mounted_config(self, hub_container):
        r = requests.get(f"http://localhost:{hub_container}/streams.json", timeout=10)
        assert r.json() == HUB_TEST_STREAMS

    def test_streams_json_content_type(self, hub_container):
        r = requests.get(f"http://localhost:{hub_container}/streams.json", timeout=10)
        assert "json" in r.headers.get("Content-Type", "").lower(), (
            f"streams.json must be served with a JSON content type, "
            f"got: {r.headers.get('Content-Type')}"
        )
