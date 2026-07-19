"""
Unit tests for /archive's window cap and concurrency guard.

/video re-encodes and is capped at 12 h; /archive only streams existing
bytes, so its cap is looser (24 h) — but it exists, and so does a
concurrency limit: each download is a sustained disk-read + network burst
on the recording host.  Staging is stubbed (gated on an Event) so the
test can hold one slot open while probing the overflow path.
"""
import http.client
import threading
import time

import pytest


@pytest.fixture
def archive_server(monkeypatch, tmp_path):
    """A live web_server on an ephemeral port with a gated staging stub."""
    monkeypatch.setenv('STREAM_WIDTH', '640')
    monkeypatch.setenv('STREAM_HEIGHT', '360')
    monkeypatch.setenv('DESKTOP_SPLITS', '640x360+0+0')
    monkeypatch.setenv('ARCHIVE_DIR', str(tmp_path / 'archive'))
    monkeypatch.setenv('ARCHIVE_LIVE_DIR', str(tmp_path / 'live'))
    (tmp_path / 'archive').mkdir()
    (tmp_path / 'live').mkdir()

    import web_server  # env must be in place before the first import

    started = threading.Event()   # an archive slot is occupied
    release = threading.Event()   # let the occupied slot finish

    real_stage = web_server.stage_segments

    def slow_stage(*args, **kwargs):
        started.set()
        release.wait(10)
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(web_server, 'ARCHIVE_DIR', str(tmp_path / 'archive'))
    monkeypatch.setattr(web_server, 'ARCHIVE_LIVE_DIR', str(tmp_path / 'live'))
    monkeypatch.setattr(web_server, 'stage_segments', slow_stage)
    monkeypatch.setattr(web_server, '_archive_slots',
                        threading.BoundedSemaphore(1))

    srv = web_server.ThreadingHTTPServer(('127.0.0.1', 0), web_server.Router)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], started, release
    finally:
        release.set()
        srv.shutdown()


def _get_archive(port, results, key, query='?last=1s'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        conn.request('GET', '/archive' + query)
        r = conn.getresponse()
        results[key] = (r.status, dict(r.getheaders()), r.read())
    finally:
        conn.close()


def test_window_over_24h_rejected(archive_server):
    port, _started, release = archive_server
    release.set()
    results = {}
    _get_archive(port, results, 'over', '?last=86401s')
    assert results['over'][0] == 400


def test_window_at_24h_accepted(archive_server):
    port, _started, release = archive_server
    release.set()
    results = {}
    _get_archive(port, results, 'edge', '?last=86400s')
    status, headers, body = results['edge']
    assert status == 200
    assert headers.get('Content-Type') == 'application/zip'
    assert int(headers['Content-Length']) == len(body)


def test_overflow_returns_503_with_retry_after(archive_server):
    port, started, release = archive_server
    results = {}

    first = threading.Thread(target=_get_archive, args=(port, results, 'first'))
    first.start()
    assert started.wait(5), 'first request never reached staging'

    _get_archive(port, results, 'second')    # slot is held -> overflow
    status, headers, body = results['second']
    assert status == 503
    assert headers.get('Retry-After', '').isdigit()
    assert b'retry' in body.lower()

    release.set()                            # let the first request finish
    first.join(timeout=10)
    status, headers, body = results['first']
    assert status == 200
    assert int(headers['Content-Length']) == len(body)


def test_slot_is_released_after_completion(archive_server):
    port, _started, release = archive_server
    release.set()                            # staging proceeds immediately
    results = {}
    for key in ('a', 'b'):                   # sequential requests both succeed
        # /archive holds its slot through streaming, and the client can see
        # the last body byte a moment before the server thread's cleanup
        # releases the slot — retry briefly on 503 instead of racing it.
        deadline = time.monotonic() + 5
        while True:
            _get_archive(port, results, key)
            if results[key][0] != 503 or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert results[key][0] == 200
