#!/usr/bin/env python3
"""
web_server.py -- HTTP router for the desktop-stream-service web UI.

Routes each configured screen path (e.g. /top, /bottom, /left, /right, or
/screen1...) to index.html so the browser's signalling-port logic can pick
the correct WebRTC server based on the runtime config.  All other paths are
served as static files from WEB_DIR.

GET /config.json
  Returns the runtime config (desktop name, capture resolution, list of
  named screen regions with their signalling ports).  Read by index.html on
  page load.

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
import json
import os
import shutil
import tempfile
import time
import urllib.parse
import zipfile
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from archive_times import parse_segment_times, renamed_segment_path
from desktop_config import load_config
from video_transcode import transcode_to_video

WEB_DIR              = os.environ.get('WEB_DIR', '/var/www/html')
ARCHIVE_DIR          = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC  = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
VIDEO_FILL_COLOR     = int(os.environ.get('VIDEO_FILL_COLOR', '0xFF000000'), 16)
VIDEO_DEFAULT_WIDTH  = int(os.environ.get('VIDEO_DEFAULT_WIDTH', '1920'))
VIDEO_DEFAULT_HEIGHT = int(os.environ.get('VIDEO_DEFAULT_HEIGHT', '1080'))
VIDEO_MAX_SEC        = 12 * 3600

CONFIG = load_config()
CONFIG_JSON = json.dumps(CONFIG).encode('utf-8')
ROUTED_PATHS = {s['path'] for s in CONFIG['screens']}


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


def stage_segments(archive_dir, start_ts, end_ts):
    """Copy overlapping segments into a TemporaryDirectory and return it.

    Completed segments have their recording timestamps embedded in their
    filenames (via the fragment-closed rename in pipeline.py); those
    timestamps are used directly.

    The current active segment (the highest-named unnamed file) is included
    when its estimated time range overlaps the request window.  Its start
    time is the end of the last completed segment; its end time is now().
    If no completed segments exist before it, the active segment is excluded
    (no reliable start time can be determined).

    The caller owns the returned TemporaryDirectory and must clean it up
    (use as a context manager or call .cleanup() explicitly).
    """
    tmp = tempfile.TemporaryDirectory(prefix='archive_stage_')
    all_files = glob.glob(os.path.join(archive_dir, '*.mkv'))
    if not all_files:
        return tmp

    now = time.time()
    renamed = []
    unnamed = []

    for path in all_files:
        times = parse_segment_times(os.path.basename(path))
        if times:
            renamed.append((path, times[0], times[1]))
        else:
            unnamed.append(path)

    renamed.sort(key=lambda x: x[1])

    for path, seg_start, seg_end in renamed:
        if seg_start < end_ts and seg_end > start_ts:
            try:
                shutil.copy2(path, os.path.join(tmp.name, os.path.basename(path)))
            except FileNotFoundError:
                pass  # purge deleted it between glob and copy; skip it

    # The active (currently-writing) segment is the highest-named unnamed
    # file.  Its start time equals the end of the last completed segment —
    # the same boundary pipeline.py will use when it finalizes the file.
    # Without a completed predecessor we have no reliable start time, so
    # any other unnamed files (orphans from crashed runs) are dropped.
    #
    # Race with segment rollover (pipeline.py calling os.rename):
    #   - If we open() the file before rename fires, the fd holds a
    #     reference to the inode; the copy completes even if the name
    #     changes underneath us.
    #   - If rename fires before open(), we get FileNotFoundError.
    #     The file now exists under its timestamp name, so we re-glob
    #     to find and copy it as a completed segment.
    if unnamed and renamed:
        active    = max(unnamed)
        seg_start = renamed[-1][2]
        seg_end   = now
        if seg_start < end_ts and seg_end > start_ts and seg_end - seg_start >= 1.0:
            prefix = os.path.basename(active).rsplit('-', 1)[0]
            dst = renamed_segment_path(
                os.path.join(tmp.name, os.path.basename(active)),
                int(seg_start * 1e9),
                int(seg_end   * 1e9),
                prefix,
            )
            try:
                with open(active, 'rb') as src, open(dst, 'wb') as out:
                    shutil.copyfileobj(src, out)
            except FileNotFoundError:
                # Rollover fired in the window before open().
                # Find the now-completed file by its start timestamp.
                for path in glob.glob(os.path.join(archive_dir, '*.mkv')):
                    times = parse_segment_times(os.path.basename(path))
                    if times and abs(times[0] - seg_start) < 1.0:
                        if times[0] < end_ts and times[1] > start_ts:
                            try:
                                shutil.copy2(path, os.path.join(
                                    tmp.name, os.path.basename(path)))
                            except FileNotFoundError:
                                pass
                        break

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
        elif path == '/config.json':
            self._handle_config()
        else:
            super().do_GET()

    def _handle_config(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(CONFIG_JSON)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(CONFIG_JSON)

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

        tmp = stage_segments(ARCHIVE_DIR, start_ts, end_ts)
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

        stage_tmp  = stage_segments(ARCHIVE_DIR, start_ts, end_ts)
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
            print(f'[video] transcode error: {exc}', flush=True)
            self.send_error(500, 'Internal server error')
        finally:
            stage_tmp.cleanup()
            output_tmp.cleanup()

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


if __name__ == '__main__':
    port = int(os.environ.get('WEB_PORT', '8080'))
    server = ThreadingHTTPServer(('', port), Router)
    server.serve_forever()
