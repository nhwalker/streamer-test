"""archive_times.py -- segment filename timestamp helpers.

Completed segments are renamed to:
  {prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS{ext}

The timestamps represent the wall-clock UTC time of the first and last
frame recorded on the desktop, derived from the GStreamer pipeline clock.
"""
import datetime
import os
import re

_STAMP_FMT = '%Y%m%d-%H%M%S'
_SEG_RE = re.compile(r'_(\d{8}-\d{6}\.\d{3})_to_(\d{8}-\d{6}\.\d{3})(\.[^.]+)$')


def format_timestamp(epoch_ns):
    """Format nanoseconds-since-epoch as YYYYMMDD-HHMMSS.SSS (UTC)."""
    dt = datetime.datetime.fromtimestamp(epoch_ns / 1e9, tz=datetime.timezone.utc)
    return f'{dt.strftime(_STAMP_FMT)}.{dt.microsecond // 1000:03d}'


def renamed_segment_path(location, start_ns, end_ns, prefix):
    """Return the new file path for a completed segment.

    location  current file path (e.g. /archive/stream-00001.mkv)
    start_ns  recording start time in nanoseconds since epoch (UTC)
    end_ns    recording end time in nanoseconds since epoch (UTC)
    prefix    configurable filename prefix (e.g. 'stream')
    """
    ext = os.path.splitext(location)[1]
    name = f'{prefix}_{format_timestamp(start_ns)}_to_{format_timestamp(end_ns)}{ext}'
    return os.path.join(os.path.dirname(location), name)


def parse_segment_times(basename):
    """Return (start_epoch_float, end_epoch_float) from a renamed filename, or None."""
    m = _SEG_RE.search(basename)
    if not m:
        return None
    utc = datetime.timezone.utc

    def _parse(s):
        dt = datetime.datetime.strptime(s, f'{_STAMP_FMT}.%f').replace(tzinfo=utc)
        return dt.timestamp()

    return _parse(m.group(1)), _parse(m.group(2))
