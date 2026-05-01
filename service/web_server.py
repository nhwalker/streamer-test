#!/usr/bin/env python3
"""
web_server.py -- HTTP router for the desktop-stream-service web UI.

Routes /top and /bottom (with or without trailing slash) to index.html so the
browser's path-aware signalling-port logic in index.html can select the correct
WebRTC signalling server for that stream.  All other paths are served as static
files from WEB_DIR.

GET /archive?start=<timestamp>&end=<timestamp>
GET /archive?last=<duration>
  Returns a zip of the .mkv segments whose recorded time overlaps the requested
  window.  The active (currently-writing) segment is included when the window
  extends past the last completed segment; its bytes are read as-is (Matroska
  streaming format is valid at any truncation point).

GET /video?start=<timestamp>&end=<timestamp>
GET /video?last=<duration>
  Returns a single .mkv covering exactly the requested window.  Segments are
  clipped to the window boundaries; any missing coverage (gaps at the edges or
  in the middle) is filled with solid VIDEO_FILL_COLOR frames.  Requests longer
  than 12 hours are rejected with 400.

  <timestamp> accepts:
    - Unix epoch seconds as a number (integer or float)
    - ISO 8601 datetime with optional timezone (Z or ±HH:MM).
      When no timezone is given, UTC is assumed.
      Examples: 2024-01-15T10:30:00Z  2024-01-15T10:30:00+05:00  2024-01-15T10:30:00

  <duration> is a number followed by a unit character:
      30s   30 seconds
      60m   60 minutes
      1.5h  1.5 hours
  end is set to now; start is computed as now − duration.

Environment variables:
  WEB_PORT             HTTP listening port              (8080)
  WEB_DIR              Static file root                 (/var/www/html)
  ARCHIVE_DIR          Directory of .mkv segments       (/archive)
  ARCHIVE_SEGMENT_SEC  Nominal segment duration         (600)
  VIDEO_FILL_COLOR     ARGB hex fill color for gaps     (0xFF000000)
  VIDEO_DEFAULT_WIDTH  Output width when no segments    (1920)
  VIDEO_DEFAULT_HEIGHT Output height when no segments   (1080)
"""
import datetime
import glob
import os
import shutil
import tempfile
import time
import urllib.parse
import zipfile
from http.server import HTTPServer, SimpleHTTPRequestHandler

from archive_times import parse_segment_times, renamed_segment_path
from video_transcode import transcode_to_video

WEB_DIR              = os.environ.get('WEB_DIR', '/var/www/html')
ARCHIVE_DIR          = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC  = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
VIDEO_FILL_COLOR     = int(os.environ.get('VIDEO_FILL_COLOR', '0xFF000000'), 16)
VIDEO_DEFAULT_WIDTH  = int(os.environ.get('VIDEO_DEFAULT_WIDTH', '1920'))
VIDEO_DEFAULT_HEIGHT = int(os.environ.get('VIDEO_DEFAULT_HEIGHT', '1080'))
VIDEO_MAX_SEC        = 12 * 3600

ROUTED_PATHS = {'/top', '/bottom'}


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


def stage_segments(archive_dir, start_ts, end_ts, segment_sec):
    """Copy overlapping segments into a TemporaryDirectory and return it.

    Completed segments have their recording timestamps embedded in their
    filenames (via the fragment-closed rename in pipeline.py); those
    timestamps are used directly.  Any file whose name does not match the
    renamed pattern (the active segment still being written, or a segment
    from a crashed run) falls back to mtime-based time-range estimation:
      - consecutive unnamed files: [mtime(N-1), mtime(N)]
      - first unnamed file after renamed ones: [last_renamed_end, mtime]
      - first unnamed file with no renamed files: [mtime - segment_sec, mtime]
      - the last unnamed file (highest mtime, i.e. the active segment):
        end = now()

    The active segment is copied as-is; matroskamux streaming format is
    valid at any truncation point.

    The caller owns the returned TemporaryDirectory and must clean it up
    (use as a context manager or call .cleanup() explicitly).
    """
    tmp = tempfile.TemporaryDirectory(prefix='archive_stage_')
    all_files = glob.glob(os.path.join(archive_dir, '*.mkv'))
    if not all_files:
        return tmp

    now = time.time()
    renamed = []   # [(path, seg_start, seg_end)] — timestamps from filename
    unnamed = []   # [path] — no embedded timestamps; use mtime estimation

    for path in all_files:
        times = parse_segment_times(os.path.basename(path))
        if times:
            renamed.append((path, times[0], times[1]))
        else:
            unnamed.append(path)

    renamed.sort(key=lambda x: x[1])       # sort by start timestamp
    unnamed.sort(key=os.path.getmtime)      # sort oldest-first by mtime

    for path, seg_start, seg_end in renamed:
        if seg_start < end_ts and seg_end > start_ts:
            shutil.copy2(path, os.path.join(tmp.name, os.path.basename(path)))

    # Mtime-based estimation for unnamed files; copy with timestamp name so
    # _build_timeline can rely purely on filename parsing.
    last_known_end = renamed[-1][2] if renamed else None
    for i, path in enumerate(unnamed):
        is_active = (i == len(unnamed) - 1)
        mtime     = os.path.getmtime(path)
        seg_end   = now if is_active else mtime
        if i == 0:
            seg_start = last_known_end if last_known_end is not None else mtime - segment_sec
        else:
            seg_start = os.path.getmtime(unnamed[i - 1])
        if seg_start < end_ts and seg_end > start_ts:
            basename = os.path.basename(path)
            prefix   = basename.rsplit('-', 1)[0]
            dst = renamed_segment_path(
                os.path.join(tmp.name, basename),
                int(seg_start * 1e9),
                int(seg_end   * 1e9),
                prefix,
            )
            shutil.copy2(path, dst)

    return tmp


def zip_segments(stage_dir, zip_path):
    """Write all .mkv files in stage_dir into a zip archive at zip_path."""
    with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(glob.glob(os.path.join(stage_dir, '*.mkv'))):
            zf.write(path, os.path.basename(path))


class Router(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/archive':
            self._handle_archive()
        elif path == '/video':
            self._handle_video()
        else:
            super().do_GET()

    def translate_path(self, path):
        # Strip query string before checking path
        clean = path.split('?', 1)[0].rstrip('/')
        if clean in ROUTED_PATHS:
            path = '/index.html'
        return super().translate_path(path)

    def _handle_archive(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if 'last' in params:
                end_ts   = time.time()
                start_ts = end_ts - parse_duration(params['last'][0])
            elif 'start' in params and 'end' in params:
                start_ts = parse_timestamp(params['start'][0])
                end_ts   = parse_timestamp(params['end'][0])
            else:
                self.send_error(400, 'provide last=<duration> or both start=<ts> and end=<ts>')
                return
        except (ValueError, IndexError):
            self.send_error(400, 'invalid parameter value')
            return

        tmp = stage_segments(ARCHIVE_DIR, start_ts, end_ts, ARCHIVE_SEGMENT_SEC)
        try:
            zip_path = os.path.join(tmp.name, '_archive.zip')
            zip_segments(tmp.name, zip_path)
            zip_size = os.path.getsize(zip_path)
            self.send_response(200)
            self.send_header('Content-Type', 'application/zip')
            self.send_header('Content-Disposition', 'attachment; filename="archive.zip"')
            self.send_header('Content-Length', str(zip_size))
            self.end_headers()
            with open(zip_path, 'rb') as fh:
                shutil.copyfileobj(fh, self.wfile, length=64 * 1024)
        finally:
            tmp.cleanup()

    def _handle_video(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            if 'last' in params:
                end_ts   = time.time()
                start_ts = end_ts - parse_duration(params['last'][0])
            elif 'start' in params and 'end' in params:
                start_ts = parse_timestamp(params['start'][0])
                end_ts   = parse_timestamp(params['end'][0])
            else:
                self.send_error(400, 'provide last=<duration> or both start=<ts> and end=<ts>')
                return
        except (ValueError, IndexError):
            self.send_error(400, 'invalid parameter value')
            return

        if end_ts - start_ts > VIDEO_MAX_SEC:
            self.send_error(400, 'requested range exceeds 12-hour maximum')
            return

        stage_tmp  = stage_segments(ARCHIVE_DIR, start_ts, end_ts, ARCHIVE_SEGMENT_SEC)
        output_tmp = tempfile.TemporaryDirectory(prefix='video_out_')
        try:
            output_path = os.path.join(output_tmp.name, 'video.mkv')
            transcode_to_video(
                stage_tmp.name, start_ts, end_ts,
                VIDEO_FILL_COLOR, output_path,
                default_width=VIDEO_DEFAULT_WIDTH,
                default_height=VIDEO_DEFAULT_HEIGHT,
            )
            video_size = os.path.getsize(output_path)
            self.send_response(200)
            self.send_header('Content-Type', 'video/x-matroska')
            self.send_header('Content-Disposition', 'attachment; filename="video.mkv"')
            self.send_header('Content-Length', str(video_size))
            self.end_headers()
            with open(output_path, 'rb') as fh:
                shutil.copyfileobj(fh, self.wfile, length=64 * 1024)
        except Exception as exc:
            self.send_error(500, str(exc))
        finally:
            stage_tmp.cleanup()
            output_tmp.cleanup()

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


if __name__ == '__main__':
    port = int(os.environ.get('WEB_PORT', '8080'))
    server = HTTPServer(('', port), Router)
    server.serve_forever()
