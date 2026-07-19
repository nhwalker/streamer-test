"""
Unit tests for the /archive//video building blocks: parse_duration and
parse_timestamp (archive_times.py), stage_segments and the stored-zip
streamer (archive_export.py).

No Docker or live HTTP server required.  The active-segment branch of
stage_segments byte-copies the live fragmented MP4 into the stage dir;
these tests inject a fake copy callable so they stay self-contained.
"""
import datetime
import io
import os
import time
import zipfile

import pytest

from archive_export import (
    _copy_active_to_stage,
    stage_segments,
    write_zip_stream,
    zip_entries,
    zip_segments,
    zip_stream_size,
)
from archive_times import parse_duration, parse_timestamp

# Reference UTC epoch for 2024-01-15 10:30:00 UTC
_REF_DT  = datetime.datetime(2024, 1, 15, 10, 30, 0, tzinfo=datetime.timezone.utc)
_REF_EPS = _REF_DT.timestamp()

SEGMENT_SEC = 600


def _fake_copy(content_marker=b'copied-mp4'):
    """Build a stub for stage_segments' _copy that writes a placeholder MP4.

    The stub takes (src_fd, dst) and writes content_marker into dst.
    Successful copies return None; failures raise OSError.  The real
    function byte-copies from the fd after a moov check; tests never
    need that.
    """
    def _stub(src_fd, dst):
        with open(dst, 'wb') as fh:
            fh.write(content_marker)
    return _stub


def _make_segment(directory, index, content=b'mp4-data', age_seconds=0):
    """Write a fake unnamed (active) live .mp4 segment file."""
    os.makedirs(str(directory), exist_ok=True)
    path = os.path.join(str(directory), f'stream-{index:05d}.mp4')
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
                            _copy=_fake_copy()) as stage_dir:
            assert os.listdir(stage_dir) == []

    def test_active_with_renamed_predecessor_included(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)  # active: starts at now-600, ends at now
        with stage_segments(archive, live, now - 300, now + 1,
                            _copy=_fake_copy()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_active_with_renamed_predecessor_excluded_when_range_before_it(self, dirs):
        archive, live = dirs
        now = time.time()
        renamed_end = now - 600
        _make_renamed_segment(archive, now - 1200, renamed_end)
        _make_segment(live, 0)  # active: starts at renamed_end
        # query window ends before the active segment starts
        with stage_segments(archive, live, 0, renamed_end - 10,
                            _copy=_fake_copy()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_orphan_unnamed_files_dropped(self, dirs):
        """Only the highest-named unnamed file is considered; others are orphans."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1800, now - 1200)
        _make_segment(live, 0)  # orphan from a crash
        _make_segment(live, 1)  # active
        with stage_segments(archive, live, now - 1800, now + 1,
                            _copy=_fake_copy()) as stage_dir:
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

    def test_active_content_copied(self, dirs):
        """Active live .mp4 is streamed verbatim into the stage dir."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0, content=b'\x00\x00\x00\x20ftypisomlive-mp4-bytes')

        marker = b'\x00\x00\x00\x20ftypisom-copied'
        # Window overlaps both renamed (ends at now-600) and active
        # (starts at now-600, ends at now).
        with stage_segments(archive, live, now - 1200, now + 1,
                            _copy=_fake_copy(marker)) as stage_dir:
            mp4s = [f for f in os.listdir(stage_dir) if f.endswith('.mp4')]
            assert len(mp4s) == 2  # renamed + copied active
            staged = []
            for fname in mp4s:
                with open(os.path.join(stage_dir, fname), 'rb') as fh:
                    staged.append(fh.read())
            assert marker in staged

    def test_active_copy_failure_is_skipped(self, dirs):
        """When the copy raises on the active segment, the partial output is removed."""
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)

        def failing_copy(src_fd, dst):
            with open(dst, 'wb') as fh:
                fh.write(b'partial')
            raise OSError('broken')

        # Window covers renamed + active, but only the renamed should
        # survive because the active copy fails.
        with stage_segments(archive, live, now - 1200, now + 1,
                            _copy=failing_copy) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_staged_files_all_have_timestamp_names(self, dirs):
        """Every file in the stage dir has the timestamp naming convention."""
        from archive_times import parse_segment_times
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)
        with stage_segments(archive, live, now - 1200, now + 1,
                            _copy=_fake_copy()) as stage_dir:
            for fname in os.listdir(stage_dir):
                if fname.endswith('.mp4'):
                    assert parse_segment_times(fname) is not None, \
                        f'{fname} has no timestamp'

    def test_completed_segments_are_hardlinked(self, dirs):
        """Staging pins the inode via os.link — no byte copy on the same fs."""
        archive, live = dirs
        now = time.time()
        path, name = _make_renamed_segment(archive, now - 1200, now - 600)
        with stage_segments(archive, live, now - 1300, now - 500) as stage_dir:
            staged = os.path.join(stage_dir, name)
            assert os.stat(staged).st_ino == os.stat(path).st_ino

    def test_stage_dir_created_on_archive_filesystem(self, dirs):
        """The stage dir must live inside archive_dir so hardlinks work."""
        archive, live = dirs
        tmp = stage_segments(archive, live, 0, time.time())
        try:
            assert os.path.dirname(tmp.name) == archive
        finally:
            tmp.cleanup()

    def test_stale_stage_dirs_are_swept(self, dirs):
        """Crash-leaked stage dirs are removed; fresh ones are left alone."""
        from archive_export import _STAGE_STALE_SEC
        archive, live = dirs
        stale = os.path.join(archive, '.archive_stage_old')
        fresh = os.path.join(archive, '.archive_stage_fresh')
        os.makedirs(stale)
        os.makedirs(fresh)
        old = time.time() - _STAGE_STALE_SEC - 60
        os.utime(stale, (old, old))
        with stage_segments(archive, live, 0, time.time()):
            pass
        assert not os.path.exists(stale)
        assert os.path.exists(fresh)

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
                            _copy=_fake_copy()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 1

    def test_mix_renamed_and_unnamed_all_in_range(self, dirs):
        archive, live = dirs
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600)
        _make_segment(live, 0)  # active
        with stage_segments(archive, live, now - 1300, now + 1,
                            _copy=_fake_copy()) as stage_dir:
            assert len([f for f in os.listdir(stage_dir) if f.endswith('.mp4')]) == 2


# ── _copy_active_to_stage (real copy implementation) ─────────────────────────

def _box(typ, body=b''):
    """Minimal ISO-BMFF box: 4-byte size + 4-byte type + body."""
    return (8 + len(body)).to_bytes(4, 'big') + typ + body


class TestCopyActiveToStage:
    """The real byte-copy gate: only files with a complete moov are staged.

    The archive's ffmpeg output writes ftyp+moov the moment a segment
    opens (empty_moov + flush_packets), but right at rotation — or if a
    build ever loses the flush option — the on-disk file can be a bare
    ftyp, which would make downstream ffmpeg fail with "moov atom not
    found".  Those files must be rejected with OSError so the caller
    skips the active segment instead of failing the whole request.
    """

    def _run_copy(self, tmp_path, content):
        src = tmp_path / 'active.mp4'
        src.write_bytes(content)
        dst = str(tmp_path / 'staged.mp4')
        fd = os.open(str(src), os.O_RDONLY)
        try:
            _copy_active_to_stage(fd, dst)
        finally:
            os.close(fd)
        return dst

    def test_full_fmp4_prefix_is_copied_verbatim(self, tmp_path):
        content = (_box(b'ftyp', b'iso5' * 4) + _box(b'moov', b'\x00' * 700)
                   + _box(b'moof', b'\x00' * 64) + _box(b'mdat', b'\x01' * 900))
        dst = self._run_copy(tmp_path, content)
        with open(dst, 'rb') as fh:
            assert fh.read() == content

    def test_truncated_trailing_fragment_is_trimmed(self, tmp_path):
        # A partial trailing box (mid-write) is dropped from the staged
        # copy so downstream ffmpeg (/video) always sees a clean fMP4.
        complete = (_box(b'ftyp') + _box(b'moov', b'\x00' * 700)
                    + _box(b'moof', b'\x00' * 64) + _box(b'mdat', b'\x01' * 300))
        content = complete + _box(b'mdat', b'\x01' * 900)[:400]
        dst = self._run_copy(tmp_path, content)
        with open(dst, 'rb') as fh:
            assert fh.read() == complete

    def test_fragmentless_copy_rejected(self, tmp_path):
        # ftyp+moov with the first fragment still incomplete: the trimmed
        # copy would contain zero frames (empty_moov holds no samples), so
        # the active segment is skipped rather than fed to ffmpeg.
        content = (_box(b'ftyp') + _box(b'moov', b'\x00' * 700)
                   + _box(b'mdat', b'\x01' * 900)[:400])
        with pytest.raises(OSError, match='fragment'):
            self._run_copy(tmp_path, content)

    def test_ftyp_only_file_rejected(self, tmp_path):
        with pytest.raises(OSError, match='moov'):
            self._run_copy(tmp_path, _box(b'ftyp', b'iso5' * 4))

    def test_empty_file_rejected(self, tmp_path):
        with pytest.raises(OSError):
            self._run_copy(tmp_path, b'')

    def test_incomplete_moov_rejected(self, tmp_path):
        # moov header present but its body extends past EOF (mid-flush).
        content = _box(b'ftyp') + _box(b'moov', b'\x00' * 700)[:100]
        with pytest.raises(OSError, match='moov'):
            self._run_copy(tmp_path, content)


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


# ── zip streaming (zip_entries / zip_stream_size / write_zip_stream) ──────────

class TestZipStream:
    """The hand-rolled stored-zip writer behind the streamed /archive
    response: announced size must equal streamed size exactly (it becomes
    the HTTP Content-Length), and the output must round-trip through a
    standard reader."""

    def _entries(self, tmp_path, files):
        for name, content in files.items():
            (tmp_path / name).write_bytes(content)
        return zip_entries(str(tmp_path))

    def _stream(self, entries):
        buf = io.BytesIO()
        write_zip_stream(entries, buf)
        return buf.getvalue()

    def test_stream_size_matches_bytes_written(self, tmp_path):
        entries = self._entries(tmp_path, {
            'stream-00000.mp4': b'a' * 1000,
            'stream-00001.mp4': b'b' * 17,
            'stream-00002.mp4': b'',
        })
        assert len(self._stream(entries)) == zip_stream_size(entries)

    def test_roundtrip_through_zipfile(self, tmp_path):
        files = {'stream-00000.mp4': b'aaa', 'stream-00001.mp4': b'bbbb'}
        data = self._stream(self._entries(tmp_path, files))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert set(zf.namelist()) == set(files)
            for name, content in files.items():
                assert zf.read(name) == content

    def test_entries_are_stored_not_deflated(self, tmp_path):
        """H.264 segments don't deflate; STORED skips the pointless CPU burn
        and makes the zip size computable up front."""
        data = self._stream(self._entries(tmp_path,
                                          {'stream-00000.mp4': b'x' * 100}))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert all(i.compress_type == zipfile.ZIP_STORED
                       for i in zf.infolist())

    def test_no_data_descriptors(self, tmp_path):
        """Flag bit 3 must be clear: STORED entries with data descriptors are
        rejected by streaming readers (e.g. Java's ZipInputStream)."""
        data = self._stream(self._entries(tmp_path,
                                          {'stream-00000.mp4': b'x' * 100}))
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert all(i.flag_bits & 0x0008 == 0 for i in zf.infolist())

    def test_empty_zip(self):
        assert zip_stream_size([]) == 22  # bare end-of-central-directory
        data = self._stream([])
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.namelist() == []

    def test_size_does_not_read_file_data(self, tmp_path):
        """zip_stream_size is header arithmetic only — safe to call before
        sending headers even for huge archives."""
        entries = self._entries(tmp_path, {'stream-00000.mp4': b'x' * 100})
        os.unlink(entries[0].path)          # size must not need the bytes
        assert zip_stream_size(entries) > 0

    def test_zip64_roundtrip(self, tmp_path, monkeypatch):
        """Force the zip64 paths with a tiny threshold and verify a standard
        reader still round-trips the archive and the size stays exact."""
        import archive_export
        monkeypatch.setattr(archive_export, '_ZIP64_LIMIT', 100)
        entries = self._entries(tmp_path, {'stream-00000.mp4': b'x' * 500,
                                           'stream-00001.mp4': b'y' * 30})
        data = self._stream(entries)
        assert len(data) == zip_stream_size(entries)
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            assert zf.read('stream-00000.mp4') == b'x' * 500
            assert zf.read('stream-00001.mp4') == b'y' * 30


# ── integration: stage → zip ──────────────────────────────────────────────────

class TestStageAndZip:

    def test_full_pipeline(self, dirs):
        archive, live = dirs
        content = b'\x00\x00\x00\x20ftypisomsegment-data'
        now = time.time()
        _make_renamed_segment(archive, now - 1200, now - 600, content=content)
        _make_segment(live, 0, content=b'active')

        tmp = stage_segments(archive, live, now - 1200, now + 1,
                             _copy=_fake_copy(b'copied-mp4'))
        try:
            zip_path = os.path.join(tmp.name, '_archive.zip')
            zip_segments(tmp.name, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                assert all(n.endswith('.mp4') for n in names)
                assert content in [zf.read(n) for n in names]
        finally:
            tmp.cleanup()


# ── _truncate_to_complete_boxes ───────────────────────────────────────────────

class TestTruncateToCompleteBoxes:
    """The staged active-segment copy must always end on a complete
    top-level box so /video's ffmpeg never sees a mid-fragment cut."""

    def _write(self, tmp_path, data):
        p = tmp_path / 'staged.mp4'
        p.write_bytes(data)
        return str(p)

    def test_complete_file_untouched(self, tmp_path):
        from archive_export import _truncate_to_complete_boxes
        data = (_box(b'ftyp', b'x' * 8) + _box(b'moov', b'y' * 24)
                + _box(b'moof') + _box(b'mdat', b'z' * 100))
        p = self._write(tmp_path, data)
        assert _truncate_to_complete_boxes(p) is True
        assert open(p, 'rb').read() == data

    def test_partial_trailing_box_dropped(self, tmp_path):
        from archive_export import _truncate_to_complete_boxes
        good = (_box(b'ftyp', b'x' * 8) + _box(b'moov', b'y' * 24)
                + _box(b'moof') + _box(b'mdat', b'q' * 40))
        partial = (100).to_bytes(4, 'big') + b'mdat' + b'z' * 20  # claims 100, has 28
        p = self._write(tmp_path, good + partial)
        assert _truncate_to_complete_boxes(p) is True
        assert open(p, 'rb').read() == good

    def test_trailing_moof_without_mdat_dropped(self, tmp_path):
        # The exact CI failure shape: the cut fell inside the first mdat,
        # leaving a complete moof whose sample data is missing.  The bare
        # moof must go too — ffmpeg rejects a moof with no mdat behind it.
        from archive_export import _truncate_to_complete_boxes
        prefix = _box(b'ftyp', b'x' * 8) + _box(b'moov', b'y' * 24)
        cut = prefix + _box(b'moof', b'm' * 32) + b'\x00\x00\x82' # partial mdat
        p = self._write(tmp_path, cut)
        assert _truncate_to_complete_boxes(p) is False
        assert open(p, 'rb').read() == prefix

    def test_bare_header_fragment_dropped(self, tmp_path):
        from archive_export import _truncate_to_complete_boxes
        good = _box(b'ftyp') + _box(b'moov', b'y' * 8)
        p = self._write(tmp_path, good + b'\x00\x00')  # 2 stray bytes
        assert _truncate_to_complete_boxes(p) is False  # no fragment kept
        assert open(p, 'rb').read() == good

    def test_size_zero_box_stops_walk(self, tmp_path):
        from archive_export import _truncate_to_complete_boxes
        good = _box(b'ftyp') + _box(b'moov', b'y' * 8)
        weird = (0).to_bytes(4, 'big') + b'mdat' + b'z' * 40  # size 0 = "to EOF"
        p = self._write(tmp_path, good + weird)
        assert _truncate_to_complete_boxes(p) is False
        assert open(p, 'rb').read() == good

    def test_staged_copy_is_truncated_end_to_end(self, tmp_path):
        """_copy_active_to_stage applies the trim to what it writes."""
        good = (_box(b'ftyp', b'x' * 8) + _box(b'moov', b'y' * 24)
                + _box(b'moof', b'q' * 8) + _box(b'mdat', b'z' * 16))
        partial = (64).to_bytes(4, 'big') + b'moof' + b'w' * 10
        src = tmp_path / 'active.mp4'
        src.write_bytes(good + partial)
        dst = str(tmp_path / 'staged.mp4')
        fd = os.open(str(src), os.O_RDONLY)
        try:
            _copy_active_to_stage(fd, dst)
        finally:
            os.close(fd)
        assert open(dst, 'rb').read() == good
