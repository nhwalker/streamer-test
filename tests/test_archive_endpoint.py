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

# Reference UTC epoch for 2024-01-15 10:30:00 UTC
_REF_DT  = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.timezone.utc)
_REF_EPS = _REF_DT.timestamp()

SEGMENT_SEC = 600


def _make_segment(directory, index, content=b'mkv-data', age_seconds=0):
    """Write a fake unnamed (active) segment file."""
    path = os.path.join(directory, f'stream-{index:05d}.mkv')
    with open(path, 'wb') as fh:
        fh.write(content)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _make_renamed_segment(directory, start_epoch, end_epoch, content=b'data'):
    """Write a fake completed segment with timestamps embedded in its name."""
    utc = datetime.timezone.utc
    fmt = '%Y%m%d-%H%M%S'
    def _fmt(e):
        d = datetime.datetime.fromtimestamp(e, tz=utc)
        return f'{d.strftime(fmt)}.{d.microsecond // 1000:03d}'
    name = f'stream_{_fmt(start_epoch)}_to_{_fmt(end_epoch)}.mkv'
    path = os.path.join(str(directory), name)
    with open(path, 'wb') as fh:
        fh.write(content)
    return path, name


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


# ── stage_segments ────────────────────────────────────────────────────────────

class TestStageSegments:

    def test_empty_directory(self, tmp_path):
        with stage_segments(str(tmp_path), 0, time.time()) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_returns_temporary_directory(self, tmp_path):
        import tempfile
        result = stage_segments(str(tmp_path), 0, time.time())
        assert isinstance(result, tempfile.TemporaryDirectory)
        result.cleanup()

    def test_cleanup_removes_stage_dir(self, tmp_path):
        tmp = stage_segments(str(tmp_path), 0, time.time())
        stage_path = tmp.name
        assert os.path.isdir(stage_path)
        tmp.cleanup()
        assert not os.path.exists(stage_path)

    def test_renamed_segment_in_range_included(self, tmp_path):
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        with stage_segments(str(tmp_path), now - 1100, now - 700) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 1

    def test_renamed_segment_outside_range_excluded(self, tmp_path):
        now = time.time()
        _make_renamed_segment(tmp_path, now - 2400, now - 1800)
        with stage_segments(str(tmp_path), now - 600, now) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_multiple_renamed_in_range(self, tmp_path):
        now = time.time()
        for i in range(4):
            _make_renamed_segment(tmp_path, now - (4 - i) * 700,
                                             now - (3 - i) * 700)
        with stage_segments(str(tmp_path), 0, now + 1) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 4

    def test_active_without_renamed_predecessor_excluded(self, tmp_path):
        """Active (unnamed) file is excluded when no completed segment precedes it."""
        _make_segment(tmp_path, 0)
        with stage_segments(str(tmp_path), 0, time.time() + 1) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_active_with_renamed_predecessor_included(self, tmp_path):
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0)  # active: starts at now-600, ends at now
        with stage_segments(str(tmp_path), now - 300, now + 1) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 1

    def test_active_with_renamed_predecessor_excluded_when_range_before_it(self, tmp_path):
        now = time.time()
        renamed_end = now - 600
        _make_renamed_segment(tmp_path, now - 1200, renamed_end)
        _make_segment(tmp_path, 0)  # active: starts at renamed_end
        # query window ends before the active segment starts
        with stage_segments(str(tmp_path), 0, renamed_end - 10) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 1

    def test_orphan_unnamed_files_dropped(self, tmp_path):
        """Only the highest-named unnamed file is considered; others are orphans."""
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1800, now - 1200)
        _make_segment(tmp_path, 0)  # orphan from a crash
        _make_segment(tmp_path, 1)  # active
        with stage_segments(str(tmp_path), now - 1800, now + 1) as stage_dir:
            # renamed + active only; orphan dropped → 2 files
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 2

    def test_renamed_content_preserved(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3renamed-mkv'
        now = time.time()
        _, name = _make_renamed_segment(tmp_path, now - 1200, now - 600, content=content)
        with stage_segments(str(tmp_path), now - 1300, now - 500) as stage_dir:
            with open(os.path.join(stage_dir, name), 'rb') as fh:
                assert fh.read() == content

    def test_active_content_preserved(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3active-mkv'
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0, content=content)
        with stage_segments(str(tmp_path), now - 300, now + 1) as stage_dir:
            all_contents = []
            for fname in os.listdir(stage_dir):
                if fname.endswith('.mkv'):
                    with open(os.path.join(stage_dir, fname), 'rb') as fh:
                        all_contents.append(fh.read())
            assert content in all_contents

    def test_staged_files_all_have_timestamp_names(self, tmp_path):
        """Every file in the stage dir has the timestamp naming convention."""
        from archive_times import parse_segment_times
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0)
        with stage_segments(str(tmp_path), now - 1200, now + 1) as stage_dir:
            for fname in os.listdir(stage_dir):
                if fname.endswith('.mkv'):
                    assert parse_segment_times(fname) is not None, \
                        f'{fname} has no timestamp'


# ── stage_segments with renamed (timestamp-in-filename) segments ─────────────

class TestStageSegmentsRenamed:
    """Explicit tests for renamed-only archive behavior."""

    def test_renamed_segment_in_range_included(self, tmp_path):
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        with stage_segments(str(tmp_path), now - 1100, now - 700) as stage_dir:
            assert len(os.listdir(stage_dir)) == 1

    def test_renamed_segment_outside_range_excluded(self, tmp_path):
        now = time.time()
        _make_renamed_segment(tmp_path, now - 2400, now - 1800)
        with stage_segments(str(tmp_path), now - 600, now) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_renamed_content_preserved(self, tmp_path):
        content = b'\x1a\x45\xdf\xa3renamed-mkv'
        now = time.time()
        _, name = _make_renamed_segment(tmp_path, now - 1200, now - 600, content=content)
        with stage_segments(str(tmp_path), now - 1300, now - 500) as stage_dir:
            with open(os.path.join(stage_dir, name), 'rb') as fh:
                assert fh.read() == content

    def test_active_segment_follows_last_renamed(self, tmp_path):
        # Renamed segment ends at now-600; active segment starts there.
        # A query covering [now-300, now+1] should get only the active segment.
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0)  # active (highest mtime)
        with stage_segments(str(tmp_path), now - 300, now + 1) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 1

    def test_mix_renamed_and_unnamed_all_in_range(self, tmp_path):
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600)
        _make_segment(tmp_path, 0)  # active
        with stage_segments(str(tmp_path), now - 1300, now + 1) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mkv')]) == 2


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
        now = time.time()
        _make_renamed_segment(tmp_path, now - 1200, now - 600, content=content)
        _make_segment(tmp_path, 0, content=b'active')

        tmp = stage_segments(str(tmp_path), now - 1200, now + 1)
        try:
            zip_path = os.path.join(tmp.name, '_archive.zip')
            zip_segments(tmp.name, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                assert content in [zf.read(n) for n in zf.namelist()]
        finally:
            tmp.cleanup()
