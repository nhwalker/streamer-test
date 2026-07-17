"""
Unit tests for archive_times.py — format_timestamp, renamed_segment_path,
and parse_segment_times.
"""
import datetime
import os


from archive_times import format_timestamp, parse_segment_times, renamed_segment_path

_UTC = datetime.timezone.utc

# Reference: 2024-01-15 10:30:00.123 UTC
_REF_DT  = datetime.datetime(2024, 1, 15, 10, 30, 0, 123000, tzinfo=_UTC)
_REF_NS  = int(_REF_DT.timestamp() * 1e9)
_REF_STR = '20240115-103000.123'


class TestFormatTimestamp:

    def test_basic(self):
        assert format_timestamp(_REF_NS) == _REF_STR

    def test_zero_milliseconds(self):
        dt = datetime.datetime(2024, 6, 1, 0, 0, 0, tzinfo=_UTC)
        ns = int(dt.timestamp() * 1e9)
        assert format_timestamp(ns) == '20240601-000000.000'

    def test_max_milliseconds(self):
        dt = datetime.datetime(2024, 12, 31, 23, 59, 59, 999000, tzinfo=_UTC)
        ns = int(dt.timestamp() * 1e9)
        assert format_timestamp(ns) == '20241231-235959.999'

    def test_microseconds_truncated_to_millis(self):
        # 500 microseconds should round down to 000 ms
        dt = datetime.datetime(2024, 1, 1, 0, 0, 0, 500, tzinfo=_UTC)
        ns = int(dt.timestamp() * 1e9)
        assert format_timestamp(ns).endswith('.000')


class TestRenamedSegmentPath:

    def test_basic(self):
        start_ns = _REF_NS
        end_ns   = _REF_NS + 600_000_000_000  # 600 s later
        path = renamed_segment_path('/archive/stream-00000.mkv', start_ns, end_ns, 'stream')
        assert os.path.dirname(path) == '/archive'
        assert path.startswith('/archive/stream_')
        assert '_to_' in path
        assert path.endswith('.mkv')

    def test_extension_preserved(self):
        path = renamed_segment_path('/archive/stream-00000.mkv', _REF_NS, _REF_NS + 1, 'stream')
        assert path.endswith('.mkv')

    def test_custom_prefix(self):
        path = renamed_segment_path('/archive/cam1-00000.mkv', _REF_NS, _REF_NS + 1, 'cam1')
        assert os.path.basename(path).startswith('cam1_')

    def test_roundtrip_with_parse(self):
        end_ns = _REF_NS + 600_000_000_000
        path = renamed_segment_path('/archive/stream-00000.mkv', _REF_NS, end_ns, 'stream')
        times = parse_segment_times(os.path.basename(path))
        assert times is not None
        start_back, end_back = times
        assert abs(start_back - _REF_DT.timestamp()) < 0.001
        assert abs(end_back - (_REF_DT.timestamp() + 600)) < 0.001

    def test_directory_preserved(self):
        path = renamed_segment_path('/my/archive/dir/stream-00000.mkv', _REF_NS, _REF_NS + 1, 'stream')
        assert os.path.dirname(path) == '/my/archive/dir'

    def test_dest_dir_overrides_source_directory(self):
        path = renamed_segment_path(
            '/archive-live/stream-00000.mkv', _REF_NS, _REF_NS + 1, 'stream',
            dest_dir='/archive',
        )
        assert os.path.dirname(path) == '/archive'
        assert os.path.basename(path).startswith('stream_')

    def test_dest_dir_none_falls_back_to_source(self):
        path = renamed_segment_path(
            '/archive-live/stream-00000.mkv', _REF_NS, _REF_NS + 1, 'stream',
            dest_dir=None,
        )
        assert os.path.dirname(path) == '/archive-live'

    def test_ext_override(self):
        """ext='.mp4' overrides the source extension (used when remuxing
        the live .mkv into the completed .mp4)."""
        path = renamed_segment_path(
            '/archive-live/stream-00000.mkv', _REF_NS, _REF_NS + 1, 'stream',
            dest_dir='/archive', ext='.mp4',
        )
        assert path.endswith('.mp4')
        assert '/archive/stream_' in path

    def test_ext_none_preserves_source(self):
        path = renamed_segment_path(
            '/archive-live/stream-00000.mkv', _REF_NS, _REF_NS + 1, 'stream',
            ext=None,
        )
        assert path.endswith('.mkv')


class TestParseSegmentTimes:

    def test_valid_filename(self):
        name = f'stream_{_REF_STR}_to_20240115-104000.000.mkv'
        times = parse_segment_times(name)
        assert times is not None
        start, end = times
        assert abs(start - _REF_DT.timestamp()) < 0.001
        end_dt = datetime.datetime(2024, 1, 15, 10, 40, 0, tzinfo=_UTC)
        assert abs(end - end_dt.timestamp()) < 0.001

    def test_numeric_filename_returns_none(self):
        assert parse_segment_times('stream-00000.mkv') is None

    def test_plain_name_returns_none(self):
        assert parse_segment_times('recording.mkv') is None

    def test_custom_prefix(self):
        name = f'cam1_{_REF_STR}_to_20240115-104000.000.mkv'
        times = parse_segment_times(name)
        assert times is not None

    def test_start_before_end(self):
        name = 'stream_20240115-100000.000_to_20240115-101000.000.mkv'
        start, end = parse_segment_times(name)
        assert start < end

    def test_no_extension_returns_none(self):
        # Regex requires a file extension after the second timestamp
        assert parse_segment_times('stream_20240115-100000.000_to_20240115-101000.000') is None
