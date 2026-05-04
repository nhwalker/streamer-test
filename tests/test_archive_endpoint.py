"""
Unit tests for parse_timestamp(), stage_segments(), and zip_segments()
in web_server.py.

No Docker, GStreamer, or live HTTP server required.

The active-segment branch of stage_segments invokes ffmpeg to remux a live
.mkv into a faststart .mp4.  These tests inject a fake remux callable so
they remain self-contained: ffmpeg is not required on the test host.
"""
import datetime
import os
import sys
import time
import types
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
from web_server import parse_duration, parse_timestamp, stage_segments, zip_segments  # noqa: E402

# Reference UTC epoch for 2024-01-15 10:30:00 UTC
_REF_DT  = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.timezone.utc)
_REF_EPS = _REF_DT.timestamp()

SEGMENT_SEC = 600


def _fake_remux(content_marker=b'remuxed-mp4'):
    """Build a stub for stage_segments' _remux that writes a placeholder MP4.

    The stub takes (src_fd, dst), writes content_marker into dst, and
    returns a CompletedProcess-like object with returncode 0.  The real
    function shells out to ffmpeg; tests never need that.
    """
    def _stub(src_fd, dst):
        with open(dst, 'wb') as fh:
            fh.write(content_marker)
        return types.SimpleNamespace(returncode=0, stderr=b'')
    return _stub


def _make_segment(directory, index, content=b'mkv-data', age_seconds=0):
    """Write a fake unnamed (active) live .mkv segment file."""
    os.makedirs(str(directory), exist_ok=True)
    path = os.path.join(str(directory), f'stream-{index:05d}.mkv')
    with open(path, 'wb') as fh:
        fh.write(content)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _make_renamed_segment(directory, start_epoch, end_epoch, content=b'data'):
    """Write a fake completed .mp4 segment with timestamps embedded in its name."""
    os.makedirs(str(directory), exist_ok=True)
    utc = datetime.timezone.utc
    fmt = '%Y%m%d-%H%M%S'
    def _fmt(e):
        d = datetime.datetime.fromtimestamp(e, tz=utc)
        return f'{d.strftime(fmt)}.{d.microsecond // 1000:03d}'
    name = f'stream_{_fmt(start_epoch)}_to_{_fmt(end_epoch)}.mp4'
    path = os.path.join(str(directory), name)
    with open(path, 'wb') as fh:
        fh.write(content)
    return path, name


@pytest.fixture
def dirs(tmp_path):
    """Return (archive_dir, live_dir) paths under a single tmp root.

    Mirrors the production layout: completed segments live in archive_dir,
    the in-progress segment lives in live_dir.
    """
    archive_dir = tmp_path / 'archive'
    live_dir    = tmp_path / 'live'
    archive_dir.mkdir()
    live_dir.mkdir()
    return str(archive_dir), str(live_dir)


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

    def test_empty_directory(self, dirs):
        archive, live = dirs
        with stage_segments(archive, live, 0, time.time()) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_returns_temporary_directory(self, dirs):
        import tempfile
        archive, live = dirs
        result = stage_segments(archive, live, 0, time.time())
        assert isinstance(result, tempfile.TemporaryDirectory)
        result.cleanup()

    def test_cleanup_removes_stage_dir(self, dirs):
        archive, live = dirs
        tmp = stage_segments(archive, live, 0, time.time())
        stage_path = tmp.name
        assert os.path.isdir(stage_path)
        tmp.cleanup()
        assert not os.path.exists(stage_path)

    def test_renamed_segment_in_range_included(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        with stage_segments(archive, live, now - 1100, now - 700) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_renamed_segment_outside_range_excluded(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 2400, now - 1800)
        with stage_segments(archive, live, now - 600, now) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_multiple_renamed_in_range(self, dirs):
        archive, live = dirs
        now = time.time()
        for i in range(4):
            _make_renamed_segment(archive, now - (4 - i) * 700,
                                           now - (3 - i) * 700)
        with stage_segments(archive, live, 0, now + 1) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 4

    def test_active_without_renamed_predecessor_excluded(self, dirs):
        """Active (unnamed) file is excluded when no completed segment precedes it."""
        archive, live = dirs
        _make_segment(live, 0)
        with stage_segments(archive, live, 0, time.time() + 1,
                            _remux=_fake_remux()) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_active_with_renamed_predecessor_included(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)  # active: starts at now-600, ends at now
        with stage_segments(archive, live, now - 300, now + 1,
                            _remux=_fake_remux()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_active_with_renamed_predecessor_excluded_when_range_before_it(self, dirs):
        archive, live = dirs
        now = time.time()
        renamed_end = now - 600
        _make_renamed_segment(archive, now - 1200, renamed_end)
        _make_segment(live, 0)  # active: starts at renamed_end
        # query window ends before the active segment starts
        with stage_segments(archive, live, 0, renamed_end - 10,
                            _remux=_fake_remux()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_orphan_unnamed_files_dropped(self, dirs):
        """Only the highest-named unnamed file is considered; others are orphans."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1800, now - 1200)
        _make_segment(live, 0)  # orphan from a crash
        _make_segment(live, 1)  # active
        with stage_segments(archive, live, now - 1800, now + 1,
                            _remux=_fake_remux()) as stage_dir:
            # renamed + active only; orphan dropped → 2 files
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 2

    def test_renamed_content_preserved(self, dirs):
        archive, live = dirs
        content = b'\x00\x00\x00\x20ftypisomrenamed-mp4'
        now = time.time()
        _, name = _make_renamed_segment(archive, now - 1200, now - 600, content=content)
        with stage_segments(archive, live, now - 1300, now - 500) as stage_dir:
            with open(os.path.join(stage_dir, name), 'rb') as fh:
                assert fh.read() == content

    def test_active_content_remuxed(self, dirs):
        """Active live .mkv is staged as a faststart .mp4 produced by the remux stub."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0, content=b'\x1a\x45\xdf\xa3live-mkv-bytes')

        marker = b'\x00\x00\x00\x20ftypisom-remuxed'
        # Window overlaps both renamed (ends at now-600) and active
        # (starts at now-600, ends at now).
        with stage_segments(archive, live, now - 1200, now + 1,
                            _remux=_fake_remux(marker)) as stage_dir:
            mp4s = [f for f in os.listdir(stage_dir) if f.endswith('.mp4')]
            assert len(mp4s) == 2  # renamed + remuxed active
            staged = []
            for fname in mp4s:
                with open(os.path.join(stage_dir, fname), 'rb') as fh:
                    staged.append(fh.read())
            assert marker in staged

    def test_active_remux_failure_is_skipped(self, dirs):
        """When ffmpeg fails on the active segment, the partial output is removed."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)

        def failing_remux(src_fd, dst):
            with open(dst, 'wb') as fh:
                fh.write(b'partial')
            return types.SimpleNamespace(returncode=1, stderr=b'broken')

        # Window covers renamed + active, but only the renamed should
        # survive because the active remux fails.
        with stage_segments(archive, live, now - 1200, now + 1,
                            _remux=failing_remux) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_staged_files_all_have_timestamp_names(self, dirs):
        """Every file in the stage dir has the timestamp naming convention."""
        from archive_times import parse_segment_times
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)
        with stage_segments(archive, live, now - 1200, now + 1,
                            _remux=_fake_remux()) as stage_dir:
            for fname in os.listdir(stage_dir):
                if fname.endswith('.mp4'):
                    assert parse_segment_times(fname) is not None, \
                        f'{fname} has no timestamp'

    def test_unnamed_files_in_archive_dir_are_ignored(self, dirs):
        """Stray unnamed files inside archive_dir (e.g. from a previous
        single-directory deployment, or a crash) must not be treated as
        the active segment."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(archive, 0)  # legacy/stray, must be ignored
        with stage_segments(archive, live, now - 300, now + 1) as stage_dir:
            # Only the renamed segment overlaps; no active in live_dir.
            assert os.listdir(stage_dir) == []


# ── stage_segments with renamed (timestamp-in-filename) segments ─────────────

class TestStageSegmentsRenamed:
    """Explicit tests for renamed-only archive behavior."""

    def test_renamed_segment_in_range_included(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        with stage_segments(archive, live, now - 1100, now - 700) as stage_dir:
            assert len(os.listdir(stage_dir)) == 1

    def test_renamed_segment_outside_range_excluded(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 2400, now - 1800)
        with stage_segments(archive, live, now - 600, now) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_renamed_content_preserved(self, dirs):
        archive, live = dirs
        content = b'\x00\x00\x00\x20ftypisomrenamed-mp4'
        now = time.time()
        _, name = _make_renamed_segment(archive, now - 1200, now - 600, content=content)
        with stage_segments(archive, live, now - 1300, now - 500) as stage_dir:
            with open(os.path.join(stage_dir, name), 'rb') as fh:
                assert fh.read() == content

    def test_active_segment_follows_last_renamed(self, dirs):
        # Renamed segment ends at now-600; active segment starts there.
        # A query covering [now-300, now+1] should get only the active segment.
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)  # active (in live_dir)
        with stage_segments(archive, live, now - 300, now + 1,
                            _remux=_fake_remux()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_mix_renamed_and_unnamed_all_in_range(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)  # active
        with stage_segments(archive, live, now - 1300, now + 1,
                            _remux=_fake_remux()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 2


# ── zip_segments ──────────────────────────────────────────────────────────────

class TestZipSegments:

    def test_empty_directory_produces_valid_empty_zip(self, tmp_path):
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == []

    def test_single_file_roundtrip(self, tmp_path):
        content = b'hello mp4'
        (tmp_path / 'stream-00000.mp4').write_bytes(content)
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.namelist() == ['stream-00000.mp4']
            assert zf.read('stream-00000.mp4') == content

    def test_multiple_files_roundtrip(self, tmp_path):
        files = {'stream-00000.mp4': b'aaa', 'stream-00001.mp4': b'bbb', 'stream-00002.mp4': b'ccc'}
        for name, content in files.items():
            (tmp_path / name).write_bytes(content)
        zip_path = str(tmp_path / 'out.zip')
        zip_segments(str(tmp_path), zip_path)
        with zipfile.ZipFile(zip_path) as zf:
            assert set(zf.namelist()) == set(files)
            for name, content in files.items():
                assert zf.read(name) == content

    def test_zip_not_included_in_itself(self, tmp_path):
        # _archive.zip lives alongside the .mp4 files; it must not appear in the zip
        (tmp_path / 'stream-00000.mp4').write_bytes(b'data')
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

    def test_full_pipeline(self, dirs):
        archive, live = dirs
        content = b'\x00\x00\x00\x20ftypisomsegment-data'
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600, content=content)
        _make_segment(live, 0, content=b'active')

        tmp = stage_segments(archive, live, now - 1200, now + 1,
                             _remux=_fake_remux(b'remuxed-mp4'))
        try:
            zip_path = os.path.join(tmp.name, '_archive.zip')
            zip_segments(tmp.name, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                assert all(n.endswith('.mp4') for n in names)
                assert content in [zf.read(n) for n in names]
        finally:
            tmp.cleanup()
