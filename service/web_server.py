#!/usr/bin/env python3
"""
web_server.py -- HTTP router for the desktop-stream-service web UI.

Routes /top and /bottom (with or without trailing slash) to index.html so the
browser's path-aware signalling-port logic in index.html can select the correct
WebRTC signalling server for that stream.  All other paths are served as static
files from WEB_DIR.

GET /archive?start=<timestamp>&end=<timestamp>
  Returns a zip of the .mkv segments whose recorded time overlaps the requested
  window.  The active (currently-writing) segment is included when the window
  extends past the last completed segment; its bytes are read as-is (Matroska
  streaming format is valid at any truncation point).

  <timestamp> accepts:
    - Unix epoch seconds as a number (integer or float)
    - ISO 8601 datetime with optional timezone (Z or ±HH:MM).
      When no timezone is given, UTC is assumed.
      Examples: 2024-01-15T10:30:00Z  2024-01-15T10:30:00+05:00  2024-01-15T10:30:00

Environment variables:
  WEB_PORT            HTTP listening port         (8080)
  WEB_DIR             Static file root            (/var/www/html)
  ARCHIVE_DIR         Directory of .mkv segments  (/archive)
  ARCHIVE_SEGMENT_SEC Nominal segment duration    (600)
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

WEB_DIR             = os.environ.get('WEB_DIR', '/var/www/html')
ARCHIVE_DIR         = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))

ROUTED_PATHS = {'/top', '/bottom'}


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

    Segment time ranges are estimated from filesystem mtimes:
      - closed segment N covers [mtime(N-1), mtime(N)]
      - the active (last) segment covers [mtime(last_closed), now()]
      - if only one segment exists its nominal start is mtime - segment_sec

    The active segment is copied as-is; matroskamux produces a valid, playable
    file at any truncation point in streaming mode.

    The caller owns the returned TemporaryDirectory and must clean it up
    (use as a context manager or call .cleanup() explicitly).
    """
    tmp = tempfile.TemporaryDirectory(prefix='archive_stage_')
    segments = sorted(glob.glob(os.path.join(archive_dir, 'stream-*.mkv')))
    if not segments:
        return tmp

    now = time.time()
    mtimes = [os.path.getmtime(p) for p in segments]

    for i, path in enumerate(segments):
        is_active = (i == len(segments) - 1)
        seg_end = now if is_active else mtimes[i]
        seg_start = mtimes[i - 1] if i > 0 else mtimes[i] - segment_sec

        if seg_start >= end_ts or seg_end <= start_ts:
            continue

        shutil.copy2(path, os.path.join(tmp.name, os.path.basename(path)))

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
        if urllib.parse.urlparse(self.path).path == '/archive':
            self._handle_archive()
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
            start_ts = parse_timestamp(params['start'][0])
            end_ts   = parse_timestamp(params['end'][0])
        except (KeyError, ValueError, IndexError):
            self.send_error(400, 'start and end must be epoch seconds or ISO 8601 timestamps')
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

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


if __name__ == '__main__':
    port = int(os.environ.get('WEB_PORT', '8080'))
    server = HTTPServer(('', port), Router)
    server.serve_forever()
