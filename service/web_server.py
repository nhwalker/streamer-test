#!/usr/bin/env python3
"""
web_server.py -- HTTP router for the desktop-stream-service web UI.

Routes each configured screen path (e.g. /top, /bottom, /left, /right, or
/screen1...) to index.html so the page can pick the matching WHEP paths out
of the runtime config.  All other paths are served as static files from
WEB_DIR.

The endpoint implementations live elsewhere; this module is request/response
glue only:

  archive_export.py    segment staging + zipping for /archive and /video
  video_transcode.py   MP4 assembly for /video
  archive_times.py     parse_duration / parse_timestamp for query params

GET /config.json
  Returns the runtime config (desktop name, capture resolution, list of
  named screen regions with their tiers and WHEP paths).  Read by
  index.html on page load.

GET /archive?start=<timestamp>&end=<timestamp>
GET /archive?last=<duration>
  Returns a zip of the .mp4 segments whose recorded time overlaps the
  requested window, including the active (currently-writing) segment when
  the window extends past the last completed segment.

GET /video?start=<timestamp>&end=<timestamp>
GET /video?last=<duration>
  Returns a single faststart .mp4 covering exactly the requested window.
  Segments are clipped to the window boundaries; any missing coverage (gaps
  at the edges or in the middle) is filled with solid VIDEO_FILL_COLOR frames.
  Requests longer than 12 hours are rejected with 400.

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
  ARCHIVE_DIR          Directory of completed .mp4      (/archive)
                       segments (timestamp-named, fragmented MP4)
  ARCHIVE_LIVE_DIR     Directory the in-progress .mp4   (/archive-live)
                       fragmented-MP4 segment is being written into
  VIDEO_FILL_COLOR     ARGB hex fill color for gaps     (0xFF000000)
  VIDEO_DEFAULT_WIDTH  Output width when no segments    (1920)
  VIDEO_DEFAULT_HEIGHT Output height when no segments   (1080)
"""
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.parse

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from archive_export import stage_segments, zip_segments
from archive_times import parse_duration, parse_timestamp
from desktop_config import load_config
from video_transcode import transcode_to_video

WEB_DIR              = os.environ.get('WEB_DIR', '/var/www/html')
ARCHIVE_DIR          = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_LIVE_DIR     = os.environ.get('ARCHIVE_LIVE_DIR', '/archive-live')
VIDEO_FILL_COLOR     = int(os.environ.get('VIDEO_FILL_COLOR', '0xFF000000'), 16)
VIDEO_DEFAULT_WIDTH  = int(os.environ.get('VIDEO_DEFAULT_WIDTH', '1920'))
VIDEO_DEFAULT_HEIGHT = int(os.environ.get('VIDEO_DEFAULT_HEIGHT', '1080'))
VIDEO_MAX_SEC        = 12 * 3600
VIDEO_MAX_CONCURRENT = int(os.environ.get('VIDEO_MAX_CONCURRENT', '2'))
VIDEO_RETRY_AFTER_SEC = 15

# Each /video request is a full ffmpeg re-encode that competes with the
# always-on live encoders for CPU (and NVENC sessions, where the budget is
# tight).  Beyond the cap, requests get 503 + Retry-After instead of
# degrading the live stream.
_video_slots = threading.BoundedSemaphore(VIDEO_MAX_CONCURRENT)

CONFIG = load_config()
CONFIG_JSON = json.dumps(CONFIG).encode('utf-8')
ROUTED_PATHS = {s['path'] for s in CONFIG['screens']}


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

    def _parse_window(self):
        """Return (start_ts, end_ts) from the request query, or None after
        having sent a 400 response."""
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
                return None
        except (ValueError, IndexError):
            self.send_error(400, 'invalid parameter value')
            return None
        return start_ts, end_ts

    def _handle_archive(self):
        window = self._parse_window()
        if window is None:
            return
        start_ts, end_ts = window

        tmp = stage_segments(ARCHIVE_DIR, ARCHIVE_LIVE_DIR, start_ts, end_ts)
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
        window = self._parse_window()
        if window is None:
            return
        start_ts, end_ts = window

        if end_ts - start_ts > VIDEO_MAX_SEC:
            self.send_error(400, 'requested range exceeds 12-hour maximum')
            return

        if not _video_slots.acquire(blocking=False):
            self.send_response(503, 'Busy')
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Retry-After', str(VIDEO_RETRY_AFTER_SEC))
            body = (f'{VIDEO_MAX_CONCURRENT} /video transcodes already '
                    'running; retry shortly\n').encode('utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            self._transcode_video(start_ts, end_ts)
        finally:
            _video_slots.release()

    def _transcode_video(self, start_ts, end_ts):
        stage_tmp  = stage_segments(ARCHIVE_DIR, ARCHIVE_LIVE_DIR, start_ts, end_ts)
        output_tmp = tempfile.TemporaryDirectory(prefix='video_out_')
        try:
            output_path = os.path.join(output_tmp.name, 'video.mp4')
            transcode_to_video(
                stage_tmp.name, start_ts, end_ts,
                VIDEO_FILL_COLOR, output_path,
                default_width=VIDEO_DEFAULT_WIDTH,
                default_height=VIDEO_DEFAULT_HEIGHT,
            )
            video_size = os.path.getsize(output_path)
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Disposition', 'attachment; filename="video.mp4"')
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
