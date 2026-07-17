"""
Unit test for the /video concurrency guard.

Each /video request runs a full ffmpeg re-encode; web_server bounds how
many run at once and answers 503 + Retry-After beyond the cap.  The
transcode is stubbed (slow, gated on an Event) so the test can hold one
slot open while probing the overflow path — no ffmpeg needed.
"""
import http.client
import threading

import pytest


@pytest.fixture
def video_server(monkeypatch, tmp_path):
    """A live web_server on an ephemeral port with a gated transcode stub."""
    monkeypatch.setenv('STREAM_WIDTH', '640')
    monkeypatch.setenv('STREAM_HEIGHT', '360')
    monkeypatch.setenv('DESKTOP_SPLITS', '640x360+0+0')
    monkeypatch.setenv('ARCHIVE_DIR', str(tmp_path / 'archive'))
    monkeypatch.setenv('ARCHIVE_LIVE_DIR', str(tmp_path / 'live'))
    (tmp_path / 'archive').mkdir()
    (tmp_path / 'live').mkdir()

    import web_server  # env must be in place before the first import

    started = threading.Event()   # a transcode slot is occupied
    release = threading.Event()   # let the occupied slot finish

    def slow_transcode(stage_dir, start_ts, end_ts, fill, out, **kw):
        started.set()
        release.wait(10)
        with open(out, 'wb') as fh:
            fh.write(b'fake-mp4')

    monkeypatch.setattr(web_server, 'ARCHIVE_DIR', str(tmp_path / 'archive'))
    monkeypatch.setattr(web_server, 'ARCHIVE_LIVE_DIR', str(tmp_path / 'live'))
    monkeypatch.setattr(web_server, 'transcode_to_video', slow_transcode)
    monkeypatch.setattr(web_server, '_video_slots',
                        threading.BoundedSemaphore(1))

    srv = web_server.ThreadingHTTPServer(('127.0.0.1', 0), web_server.Router)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], started, release
    finally:
        release.set()
        srv.shutdown()


def _get_video(port, results, key):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=15)
    try:
        conn.request('GET', '/video?last=1s')
        r = conn.getresponse()
        results[key] = (r.status, dict(r.getheaders()), r.read())
    finally:
        conn.close()


def test_overflow_returns_503_with_retry_after(video_server):
    port, started, release = video_server
    results = {}

    first = threading.Thread(target=_get_video, args=(port, results, 'first'))
    first.start()
    assert started.wait(5), 'first request never reached the transcode'

    _get_video(port, results, 'second')      # slot is held -> overflow
    status, headers, body = results['second']
    assert status == 503
    assert headers.get('Retry-After', '').isdigit()
    assert b'retry' in body.lower()

    release.set()                            # let the first request finish
    first.join(timeout=10)
    assert results['first'][0] == 200
    assert results['first'][2] == b'fake-mp4'


def test_slot_is_released_after_completion(video_server):
    port, started, release = video_server
    release.set()                            # transcodes finish immediately
    results = {}
    for key in ('a', 'b'):                   # sequential requests both succeed
        _get_video(port, results, key)
        assert results[key][0] == 200
