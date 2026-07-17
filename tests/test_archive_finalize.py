"""
Unit tests for archive_finalize.SegmentFinalizer and next_segment_number.

The finalizer tails ffmpeg's CSV segment list (name,start_s,end_s in
stream time) and publishes completed segments from the live dir into the
archive dir under timestamped names.  No ffmpeg required — the tests
write the CSV by hand.
"""
import os

import pytest

from archive_finalize import SegmentFinalizer, next_segment_number
from archive_times import parse_segment_times

ANCHOR = 1_700_000_000.0  # arbitrary fixed epoch anchor


@pytest.fixture
def dirs(tmp_path):
    live = tmp_path / 'live'
    archive = tmp_path / 'archive'
    live.mkdir()
    archive.mkdir()
    return str(live), str(archive)


def _finalizer(live, archive, prefix='stream'):
    return SegmentFinalizer(os.path.join(live, 'segments.csv'),
                            live, archive, prefix, ANCHOR)


def _write_segment(live, name, content=b'mp4-bytes'):
    with open(os.path.join(live, name), 'wb') as fh:
        fh.write(content)


def _append_csv(live, line):
    with open(os.path.join(live, 'segments.csv'), 'a') as fh:
        fh.write(line)


class TestSegmentFinalizer:

    def test_no_list_file_is_a_noop(self, dirs):
        live, archive = dirs
        assert _finalizer(live, archive).poll() == 0

    def test_completed_segment_is_published_with_timestamps(self, dirs):
        live, archive = dirs
        _write_segment(live, 'stream-00000.mp4', b'segment-zero')
        _append_csv(live, 'stream-00000.mp4,0.000000,600.000000\n')

        fin = _finalizer(live, archive)
        assert fin.poll() == 1

        # Source moved out of the live dir …
        assert not os.path.exists(os.path.join(live, 'stream-00000.mp4'))
        # … and published under a timestamp name matching anchor+stream time.
        published = [f for f in os.listdir(archive) if f.endswith('.mp4')]
        assert len(published) == 1
        start, end = parse_segment_times(published[0])
        assert start == pytest.approx(ANCHOR, abs=0.01)
        assert end == pytest.approx(ANCHOR + 600, abs=0.01)
        with open(os.path.join(archive, published[0]), 'rb') as fh:
            assert fh.read() == b'segment-zero'

    def test_poll_is_incremental(self, dirs):
        live, archive = dirs
        _write_segment(live, 'stream-00000.mp4')
        _append_csv(live, 'stream-00000.mp4,0.0,600.0\n')
        fin = _finalizer(live, archive)
        assert fin.poll() == 1
        assert fin.poll() == 0  # nothing new

        _write_segment(live, 'stream-00001.mp4')
        _append_csv(live, 'stream-00001.mp4,600.0,1200.0\n')
        assert fin.poll() == 1
        assert len(os.listdir(archive)) == 2

    def test_partial_trailing_line_is_deferred(self, dirs):
        live, archive = dirs
        _write_segment(live, 'stream-00000.mp4')
        _append_csv(live, 'stream-00000.mp4,0.0,600.0\n'
                          'stream-00001.mp4,600.0,')  # mid-write
        fin = _finalizer(live, archive)
        assert fin.poll() == 1  # only the complete line

        _write_segment(live, 'stream-00001.mp4')
        _append_csv(live, '1200.0\n')  # ffmpeg finishes the line
        assert fin.poll() == 1
        assert len(os.listdir(archive)) == 2

    def test_no_part_files_visible_in_archive(self, dirs):
        live, archive = dirs
        _write_segment(live, 'stream-00000.mp4')
        _append_csv(live, 'stream-00000.mp4,0.0,600.0\n')
        _finalizer(live, archive).poll()
        assert not any(f.endswith('.part') for f in os.listdir(archive))

    def test_missing_source_file_is_skipped_without_crash(self, dirs):
        live, archive = dirs
        # CSV references a segment that never landed (crash mid-write).
        _append_csv(live, 'stream-00000.mp4,0.0,600.0\n')
        fin = _finalizer(live, archive)
        fin.poll()          # must not raise
        assert os.listdir(archive) == []

    def test_malformed_lines_are_skipped(self, dirs):
        live, archive = dirs
        _write_segment(live, 'stream-00001.mp4')
        _append_csv(live, 'garbage-without-commas\n'
                          'stream-00000.mp4,not-a-number,9\n'
                          'stream-00001.mp4,0.0,600.0\n')
        fin = _finalizer(live, archive)
        assert fin.poll() == 1
        assert len(os.listdir(archive)) == 1

    def test_csv_paths_may_carry_directories(self, dirs):
        live, archive = dirs
        _write_segment(live, 'stream-00000.mp4')
        _append_csv(live, f'{live}/stream-00000.mp4,0.0,600.0\n')
        assert _finalizer(live, archive).poll() == 1


class TestNextSegmentNumber:

    def test_empty_dir_starts_at_zero(self, dirs):
        live, _ = dirs
        assert next_segment_number(live, 'stream') == 0

    def test_missing_dir_starts_at_zero(self, tmp_path):
        assert next_segment_number(str(tmp_path / 'nope'), 'stream') == 0

    def test_orphans_advance_the_start_number(self, dirs):
        live, _ = dirs
        _write_segment(live, 'stream-00000.mp4')
        _write_segment(live, 'stream-00007.mp4')
        assert next_segment_number(live, 'stream') == 8

    def test_other_prefixes_and_files_ignored(self, dirs):
        live, _ = dirs
        _write_segment(live, 'other-00042.mp4')
        _write_segment(live, 'segments.csv', b'x')
        assert next_segment_number(live, 'stream') == 0
