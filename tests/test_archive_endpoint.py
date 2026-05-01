"""
Unit tests for parse_timestamp(), stage_segments(), and zip_segments()
in web_server.py.

No Docker, GStreamer, or live HTTP server required.
"""
import datetime
import os
import sys
import time
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
from web_server import parse_duration, parse_timestamp, stage_segments, zip_segments  # noqa: E402

SEGMENT_SEC = 600

# Reference UTC epoch for 2024-01-15 10:30:00 UTC
_REF_DT  = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.timezone.utc)
_REF_EPS = _REF_DT.timestamp()


class TestParseDuration:

    def test_seconds(self):
        assert parse_duration('30s') == 30.0

    def test_minutes(self):
        assert parse_duration('60m') == 3600.0

    def test_hours(self):
        assert parse_duration('1.5h') == pytest.approx(5400.0)

    def test_fractional_seconds(self):
        assert parse_duration('0.5s') == pytest.approx(0.5)

    def test_fractional_minutes(self):
        assert parse_duration('1.5m') == pytest.approx(90.0)

    def test_integer_hours(self):
        assert parse_duration('2h') == 7200.0

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError):
            parse_duration('30x')

    def test_no_unit_raises(self):
        with pytest.raises(ValueError):
            parse_duration('30')

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            parse_duration('')

    def test_non_numeric_value_raises(self):
        with pytest.raises(ValueError):
            parse_duration('abcs')


class TestParseTimestamp:

    def test_integer_string(self):
        assert parse_timestamp('1234567890') == 1234567890.0

    def test_float_string(self):
        assert parse_timestamp('1234567890.5') == pytest.approx(1234567890.5)

    def test_iso_with_z(self):
        assert parse_timestamp('2024-01-15T10:30:00Z') == pytest.approx(_REF_EPS)

    def test_iso_without_timezone_assumed_utc(self):
        assert parse_timestamp('2024-01-15T10:30:00') == pytest.approx(_REF_EPS)

    def test_iso_with_positive_offset(self):
        # +05:00 means the wall time is 5 h ahead of UTC, so UTC is 5 h earlier
        assert parse_timestamp('2024-01-15T15:30:00+05:00') == pytest.approx(_REF_EPS)

    def test_iso_with_negative_offset(self):
        assert parse_timestamp('2024-01-15T05:30:00-05:00') == pytest.approx(_REF_EPS)

    def test_iso_date_only_assumed_utc_midnight(self):
        midnight = datetime.datetime(2024, 1, 15, tzinfo=datetime.timezone.utc).timestamp()
        assert parse_timestamp('2024-01-15') == pytest.approx(midnight)

    def test_iso_with_fractional_seconds(self):
        ref = _REF_EPS + 0.123
        assert parse_timestamp('2024-01-15T10:30:00.123Z') == pytest.approx(ref, abs=1e-3)

    def test_invalid_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_timestamp('not-a-timestamp')

    def test_invalid_date_raises_value_error(self):
        with pytest.raises(ValueError):
            parse_timestamp('2024-13-01T00:00:00')


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


# ── stage_segments with renamed (timestamp-in-filename) segments ─────────────

class TestStageSegmentsRenamed:
    """stage_segments uses filename timestamps for completed (renamed) segments."""

    def _make_renamed(self, directory, start_epoch, end_epoch, content=b'data'):
        import datetime as _dt
        utc = _dt.timezone.utc
        fmt = '%Y%m%d-%H%M%S'
        def _fmt(e):
            d = _dt.datetime.fromtimestamp(e, tz=utc)
            return f'{d.strftime(fmt)}.{d.microsecond // 1000:03d}'
        name = f'stream_{_fmt(start_epoch)}_to_{_fmt(end_epoch)}.mkv'
        path = os.path.join(str(directory), name)
        with open(path, 'wb') as fh:
            fh.write(content)
        return path, name

    def test_renamed_segment_in_range_included(self, tmp_path):
        now = time.time()
        self._make_renamed(tmp_path, now - 1200, now - 600)
        with stage_segments(str(tmp_path), now - 1100, now - 700, SEGMENT_SEC) as stage_dir:
            assert len(os.listdir(stage_dir)) == 1

    def test_renamed_segment_outside_range_excluded(self, tmp_path):
        now = time.time()
        self._make_renamed(tmp_path, now - 2400, now - 1800)
        with stage_segments(str(tmp_path), now - 600, now, SEGMENT_SEC) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_renamed_content_preserved(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3renamed-mkv'
        now = time.time()
        _, name = self._make_renamed(tmp_path, now - 1200, now - 600, content=content)
        with stage_segments(str(tmp_path), now - 1300, now - 500, SEGMENT_SEC) as stage_dir:
            with open(os.path.join(stage_dir, name), 'rb') as fh:
                assert fh.read() == content

    def test_active_segment_follows_last_renamed(self, tmp_path):
        # Renamed segment ends at now-600; active segment starts there.
        # A query covering [now-300, now+1] should get only the active segment.
        now = time.time()
        self._make_renamed(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0, age_seconds=1)  # active (highest mtime)
        with stage_segments(str(tmp_path), now - 300, now + 1, SEGMENT_SEC) as stage_dir:
            files = os.listdir(stage_dir)
            assert 'stream-00000.mkv' in files
            assert len(files) == 1

    def test_mix_renamed_and_unnamed_all_in_range(self, tmp_path):
        now = time.time()
        self._make_renamed(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0, age_seconds=1)   # active
        with stage_segments(str(tmp_path), now - 1300, now + 1, SEGMENT_SEC) as stage_dir:
            assert len(os.listdir(stage_dir)) == 2


# ── zip_segments ──────────────────────────────────────────────────────────────

class TestZipSegments:

    def test_empty_directory_produces_valid_empty_zip(self, tmp_path):
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == []

    def test_single_file_roundtrip(self, tmp_path):
        content = b'hello mkv'
        (tmp_path / 'stream-00000.mkv').write_bytes(content)
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == ['stream-00000.mkv']
            assert zf.read('stream-00000.mkv') == content

    def test_multiple_files_roundtrip(self, tmp_path):
        files = {'stream-00000.mkv': b'aaa', 'stream-00001.mkv': b'bbb', 'stream-00002.mkv': b'ccc'}
        for name, content in files.items():
            (tmp_path / name).write_bytes(content)
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert set(zf.namelist()) == set(files)
            for name, content in files.items():
                assert zf.read(name) == content

    def test_zip_not_included_in_itself(self, tmp_path):
        # _archive.zip lives alongside the .mkv files; it must not appear in the zip
        (tmp_path / 'stream-00000.mkv').write_bytes(b'data')
        zip_path = str(tmp_path / '_archive.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert '_archive.zip' not in zf.namelist()

    def test_writes_to_disk(self, tmp_path):
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        assert os.path.isfile(zip_path)


# ── integration: stage → zip ──────────────────────────────────────────────────

class TestStageAndZip:

    def test_full_pipeline(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3segment-data'
        _make_segment(tmp_path, 0, content=content, age_seconds=10)
        _make_segment(tmp_path, 1, content=b'active', age_seconds=0)

        now = time.time()
        tmp = stage_segments(str(tmp_path), now - 30, now + 1, SEGMENT_SEC)
        try:
            zip_path = os.path.join(tmp.name, '_archive.zip')
            zip_segments(tmp.name, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                assert 'stream-00000.mkv' in zf.namelist()
                assert zf.read('stream-00000.mkv') == content
        finally:
            tmp.cleanup()
