"""
Unit tests for stage_segments() and zip_segments() in web_server.py.

No Docker, GStreamer, or live HTTP server required.
"""
import io
import os
import sys
import time
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
from web_server import stage_segments, zip_segments  # noqa: E402

SEGMENT_SEC = 600


def _make_segment(directory, index, content=b'mkv-data', age_seconds=0):
    """Write a fake segment file and set its mtime to now - age_seconds."""
    path = os.path.join(directory, f'stream-{index:05d}.mkv')
    with open(path, 'wb') as fh:
        fh.write(content)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


# ── stage_segments ────────────────────────────────────────────────────────────

class TestStageSegments:

    def test_empty_directory(self, tmp_path):
        with stage_segments(str(tmp_path), 0, time.time(), SEGMENT_SEC) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_returns_temporary_directory(self, tmp_path):
        import tempfile
        result = stage_segments(str(tmp_path), 0, time.time(), SEGMENT_SEC)
        assert isinstance(result, tempfile.TemporaryDirectory)
        result.cleanup()

    def test_cleanup_removes_stage_dir(self, tmp_path):
        tmp = stage_segments(str(tmp_path), 0, time.time(), SEGMENT_SEC)
        stage_path = tmp.name
        assert os.path.isdir(stage_path)
        tmp.cleanup()
        assert not os.path.exists(stage_path)

    def test_only_active_segment_included_when_range_covers_now(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=60)
        now = time.time()
        with stage_segments(str(tmp_path), now - 120, now + 1, SEGMENT_SEC) as stage_dir:
            assert os.listdir(stage_dir) == ['stream-00000.mkv']

    def test_only_active_segment_uses_nominal_start(self, tmp_path):
        # Active-only segment: estimated start = mtime - SEGMENT_SEC.
        # A query window entirely before that start gets nothing.
        _make_segment(tmp_path, 0, age_seconds=0)
        mtime = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00000.mkv'))
        with stage_segments(str(tmp_path), 0, mtime - SEGMENT_SEC - 1, SEGMENT_SEC) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_closed_segment_in_range(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=2400)
        _make_segment(tmp_path, 1, age_seconds=1800)
        _make_segment(tmp_path, 2, age_seconds=1200)

        mtime0 = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00000.mkv'))
        mtime1 = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00001.mkv'))

        start = mtime0 + 1
        end   = mtime1 - 1
        with stage_segments(str(tmp_path), start, end, SEGMENT_SEC) as stage_dir:
            assert os.listdir(stage_dir) == ['stream-00001.mkv']

    def test_active_segment_included_when_range_extends_past_last_closed(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=700)
        _make_segment(tmp_path, 1, age_seconds=100)  # active

        now = time.time()
        with stage_segments(str(tmp_path), now - 50, now + 1, SEGMENT_SEC) as stage_dir:
            assert 'stream-00001.mkv' in os.listdir(stage_dir)

    def test_active_segment_excluded_when_range_ends_before_it_starts(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=1300)  # closed
        _make_segment(tmp_path, 1, age_seconds=700)   # active

        mtime0 = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00000.mkv'))
        with stage_segments(str(tmp_path), 0, mtime0 - 100, SEGMENT_SEC) as stage_dir:
            assert 'stream-00001.mkv' not in os.listdir(stage_dir)

    def test_staged_file_content_matches_source(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3fake-mkv-content'
        _make_segment(tmp_path, 0, content=content, age_seconds=10)
        _make_segment(tmp_path, 1, content=b'active', age_seconds=0)

        now = time.time()
        with stage_segments(str(tmp_path), now - 20, now + 1, SEGMENT_SEC) as stage_dir:
            staged_path = os.path.join(stage_dir, 'stream-00000.mkv')
            with open(staged_path, 'rb') as fh:
                assert fh.read() == content

    def test_multiple_segments_all_in_range(self, tmp_path):
        for i in range(4):
            _make_segment(tmp_path, i, age_seconds=(4 - i) * 700)

        now = time.time()
        with stage_segments(str(tmp_path), 0, now + 1, SEGMENT_SEC) as stage_dir:
            assert len(os.listdir(stage_dir)) == 4


# ── zip_segments ──────────────────────────────────────────────────────────────

class TestZipSegments:

    def test_empty_directory_produces_valid_empty_zip(self, tmp_path):
        data = zip_segments(str(tmp_path))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == []

    def test_single_file_roundtrip(self, tmp_path):
        content = b'hello mkv'
        path = tmp_path / 'stream-00000.mkv'
        path.write_bytes(content)
        data = zip_segments(str(tmp_path))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ['stream-00000.mkv']
            assert zf.read('stream-00000.mkv') == content

    def test_multiple_files_roundtrip(self, tmp_path):
        files = {'stream-00000.mkv': b'aaa', 'stream-00001.mkv': b'bbb', 'stream-00002.mkv': b'ccc'}
        for name, content in files.items():
            (tmp_path / name).write_bytes(content)
        data = zip_segments(str(tmp_path))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert set(zf.namelist()) == set(files)
            for name, content in files.items():
                assert zf.read(name) == content

    def test_returns_bytes(self, tmp_path):
        assert isinstance(zip_segments(str(tmp_path)), bytes)


# ── integration: stage → zip ──────────────────────────────────────────────────

class TestStageAndZip:

    def test_full_pipeline(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3segment-data'
        _make_segment(tmp_path, 0, content=content, age_seconds=10)
        _make_segment(tmp_path, 1, content=b'active', age_seconds=0)

        now = time.time()
        tmp = stage_segments(str(tmp_path), now - 30, now + 1, SEGMENT_SEC)
        try:
            zip_data = zip_segments(tmp.name)
        finally:
            tmp.cleanup()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            assert 'stream-00000.mkv' in zf.namelist()
            assert zf.read('stream-00000.mkv') == content
