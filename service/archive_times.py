"""archive_times.py -- time parsing/formatting for the archive subsystem.

Completed segments are renamed to:
  {prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS{ext}

The timestamps represent the wall-clock UTC time of the first and last
frame recorded on the desktop, derived from the epoch anchor taken when
the ffmpeg run started (see archive_finalize.py).

Also home to the user-facing time parsers for the /archive and /video
request parameters (parse_duration, parse_timestamp).
"""
import datetime
import os
import re

_STAMP_FMT = '%Y%m%d-%H%M%S'
_SEG_RE = re.compile(r'_(\d{8}-\d{6}\.\d{3})_to_(\d{8}-\d{6}\.\d{3})(\.[^.]+)$')

_DURATION_UNITS = {'s': 1, 'm': 60, 'h': 3600}


def parse_duration(s):
    """Parse a duration string into seconds (float).

    Format: a number followed by a unit character: s (seconds), m (minutes),
    or h (hours).  Examples: '30s', '60m', '1.5h'.
    """
    if not s or s[-1] not in _DURATION_UNITS:
        raise ValueError(f'invalid duration {s!r}: must end with s, m, or h')
    return float(s[:-1]) * _DURATION_UNITS[s[-1]]


def parse_timestamp(s):
    """Return a UTC epoch float parsed from s.

    Accepts a numeric epoch string or any ISO 8601 datetime string.
    When no timezone is present the value is assumed to be UTC.
    """
    try:
        return float(s)
    except ValueError:
        pass
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def format_timestamp(epoch_ns):
    """Format nanoseconds-since-epoch as YYYYMMDD-HHMMSS.SSS (UTC)."""
    dt = datetime.datetime.fromtimestamp(epoch_ns / 1e9, tz=datetime.timezone.utc)
    return f'{dt.strftime(_STAMP_FMT)}.{dt.microsecond // 1000:03d}'


def renamed_segment_path(location, start_ns, end_ns, prefix,
                         dest_dir=None, ext=None):
    """Return the new file path for a completed segment.

    location  current file path (e.g. /archive-live/stream-00001.mp4)
    start_ns  recording start time in nanoseconds since epoch (UTC)
    end_ns    recording end time in nanoseconds since epoch (UTC)
    prefix    configurable filename prefix (e.g. 'stream')
    dest_dir  directory the renamed file should land in; when None, the
              renamed file stays alongside the source (legacy behaviour).
    ext       output extension including the leading dot (e.g. '.mp4').
              When None, the source's extension is preserved.
    """
    if ext is None:
        ext = os.path.splitext(location)[1]
    name = f'{prefix}_{format_timestamp(start_ns)}_to_{format_timestamp(end_ns)}{ext}'
    if dest_dir is None:
        dest_dir = os.path.dirname(location)
    return os.path.join(dest_dir, name)


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
