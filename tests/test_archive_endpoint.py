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
        assert stage_segments(str(tmp_path), 0, time.time(), SEGMENT_SEC) == []

    def test_only_active_segment_included_when_range_covers_now(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=60)
        now = time.time()
        result = stage_segments(str(tmp_path), now - 120, now + 1, SEGMENT_SEC)
        assert len(result) == 1
        assert result[0][0] == 'stream-00000.mkv'

    def test_only_active_segment_uses_nominal_start(self, tmp_path):
        # Active-only segment: its estimated start = mtime - SEGMENT_SEC.
        # A query window entirely before that estimated start gets nothing.
        _make_segment(tmp_path, 0, age_seconds=0)
        mtime = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00000.mkv'))
        # Query ends before the segment nominally started
        result = stage_segments(str(tmp_path), 0, mtime - SEGMENT_SEC - 1, SEGMENT_SEC)
        assert result == []

    def test_closed_segment_in_range(self, tmp_path):
        # Three segments: 0 (2400 s ago), 1 (1800 s ago), 2 active (1200 s ago).
        # Query window covers only segment 1.
        _make_segment(tmp_path, 0, age_seconds=2400)
        _make_segment(tmp_path, 1, age_seconds=1800)
        _make_segment(tmp_path, 2, age_seconds=1200)

        mtime0 = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00000.mkv'))
        mtime1 = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00001.mkv'))

        # Window sits squarely inside segment 1's time range [mtime0, mtime1]
        start = mtime0 + 1
        end   = mtime1 - 1

        result = stage_segments(str(tmp_path), start, end, SEGMENT_SEC)
        names = [r[0] for r in result]
        assert names == ['stream-00001.mkv']

    def test_active_segment_included_when_range_extends_past_last_closed(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=700)
        _make_segment(tmp_path, 1, age_seconds=100)  # active

        now = time.time()
        result = stage_segments(str(tmp_path), now - 50, now + 1, SEGMENT_SEC)
        names = [r[0] for r in result]
        assert 'stream-00001.mkv' in names

    def test_active_segment_excluded_when_range_ends_before_it_starts(self, tmp_path):
        _make_segment(tmp_path, 0, age_seconds=1300)  # closed
        _make_segment(tmp_path, 1, age_seconds=700)   # active

        mtime0 = os.path.getmtime(os.path.join(str(tmp_path), 'stream-00000.mkv'))
        # Window ends before segment 0 even closed — nothing should match
        result = stage_segments(str(tmp_path), 0, mtime0 - 100, SEGMENT_SEC)
        # Segment 0 covers [mtime0 - SEGMENT_SEC, mtime0]; window ends at mtime0-100
        # so segment 0 IS included; segment 1 is not.
        names = [r[0] for r in result]
        assert 'stream-00001.mkv' not in names

    def test_returned_bytes_match_file_content(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3fake-mkv-content'
        _make_segment(tmp_path, 0, content=content, age_seconds=10)
        _make_segment(tmp_path, 1, content=b'active', age_seconds=0)

        now = time.time()
        result = stage_segments(str(tmp_path), now - 20, now + 1, SEGMENT_SEC)
        data = {name: data for name, data in result}
        assert data.get('stream-00000.mkv') == content

    def test_multiple_segments_all_in_range(self, tmp_path):
        for i in range(4):
            _make_segment(tmp_path, i, age_seconds=(4 - i) * 700)

        now = time.time()
        result = stage_segments(str(tmp_path), 0, now + 1, SEGMENT_SEC)
        assert len(result) == 4


# ── zip_segments ──────────────────────────────────────────────────────────────

class TestZipSegments:

    def test_empty_staged_produces_valid_empty_zip(self):
        data = zip_segments([])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == []

    def test_single_file_roundtrip(self):
        content = b'hello mkv'
        data = zip_segments([('stream-00000.mkv', content)])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == ['stream-00000.mkv']
            assert zf.read('stream-00000.mkv') == content

    def test_multiple_files_roundtrip(self):
        staged = [
            ('stream-00000.mkv', b'aaa'),
            ('stream-00001.mkv', b'bbb'),
            ('stream-00002.mkv', b'ccc'),
        ]
        data = zip_segments(staged)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert set(zf.namelist()) == {
                'stream-00000.mkv', 'stream-00001.mkv', 'stream-00002.mkv'
            }
            for name, content in staged:
                assert zf.read(name) == content

    def test_returns_bytes(self):
        assert isinstance(zip_segments([]), bytes)


# ── integration: stage → zip ──────────────────────────────────────────────────

class TestStageAndZip:

    def test_full_pipeline(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3segment-data'
        _make_segment(tmp_path, 0, content=content, age_seconds=10)
        _make_segment(tmp_path, 1, content=b'active', age_seconds=0)

        now = time.time()
        staged   = stage_segments(str(tmp_path), now - 30, now + 1, SEGMENT_SEC)
        zip_data = zip_segments(staged)

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            assert 'stream-00000.mkv' in zf.namelist()
            assert zf.read('stream-00000.mkv') == content
