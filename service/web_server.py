#!/usr/bin/env python3
"""
web_server.py -- HTTP router for the desktop-stream-service web UI.

Routes each configured screen path (e.g. /top, /bottom, /left, /right, or
/screen1...) to index.html so the page can pick the matching WHEP paths out
of the runtime config.  All other paths are served as static files from
WEB_DIR.

The endpoint implementations live elsewhere; this module is request/response
glue only:

  archive_export.py    segment staging for /archive and /video
  zip_stream.py        stored-zip streaming (exact size up front) for /archive
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
  the window extends past the last completed segment.  The zip is streamed
  (stored entries, exact Content-Length, nothing written to disk).
  Requests longer than 24 hours are rejected with 400; at most
  ARCHIVE_MAX_CONCURRENT downloads run at once (503 + Retry-After beyond).

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
  ARCHIVE_MAX_CONCURRENT  Max simultaneous /archive downloads  (2)
  VIDEO_FILL_COLOR     ARGB hex fill color for gaps     (0xFF000000)
  VIDEO_DEFAULT_WIDTH  Output width when no segments    (1920)
  VIDEO_DEFAULT_HEIGHT Output height when no segments   (1080)
"""
import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
import urllib.parse

from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

from archive_export import stage_segments, sweep_stage_dirs, zip_entries
from archive_times import parse_duration, parse_timestamp
from desktop_config import load_config
from video_transcode import transcode_to_video
from zip_stream import write_zip_stream, zip_stream_size

WEB_DIR              = os.environ.get('WEB_DIR', '/var/www/html')
ARCHIVE_DIR          = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_LIVE_DIR     = os.environ.get('ARCHIVE_LIVE_DIR', '/archive-live')
ARCHIVE_MAX_SEC      = 24 * 3600
ARCHIVE_MAX_CONCURRENT = int(os.environ.get('ARCHIVE_MAX_CONCURRENT', '2'))
VIDEO_FILL_COLOR     = int(os.environ.get('VIDEO_FILL_COLOR', '0xFF000000'), 16)
VIDEO_DEFAULT_WIDTH  = int(os.environ.get('VIDEO_DEFAULT_WIDTH', '1920'))
VIDEO_DEFAULT_HEIGHT = int(os.environ.get('VIDEO_DEFAULT_HEIGHT', '1080'))
VIDEO_MAX_SEC        = 12 * 3600
VIDEO_MAX_CONCURRENT = int(os.environ.get('VIDEO_MAX_CONCURRENT', '2'))
RETRY_AFTER_SEC      = 15

# Each /video request is a full ffmpeg re-encode that competes with the
# always-on live encoders for CPU (and NVENC sessions, where the budget is
# tight).  Beyond the cap, requests get 503 + Retry-After instead of
# degrading the live stream.
_video_slots = threading.BoundedSemaphore(VIDEO_MAX_CONCURRENT)

# /archive is much cheaper per hour than /video (no re-encode), but each
# download is still a sustained disk-read + network burst on the box that
# is also recording; cap how many run at once so a batch of downloads
# cannot starve the recorder's disk bandwidth.
_archive_slots = threading.BoundedSemaphore(ARCHIVE_MAX_CONCURRENT)


@contextlib.contextmanager
def _holding(slot):
    """Release an already-acquired semaphore slot when the block exits.

    Handlers acquire non-blocking first (the failure path sends a 503),
    then wrap the work the slot is meant to cover in `with _holding(...)`
    so the slot's scope is visible syntactically.
    """
    try:
        yield
    finally:
        slot.release()

CONFIG = load_config()
CONFIG_JSON = json.dumps(CONFIG).encode('utf-8')
ROUTED_PATHS = {s['path'] for s in CONFIG['screens']}


class Router(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == '/archive':
            self._handle_archive(parsed.query)
        elif parsed.path == '/video':
            self._handle_video(parsed.query)
        elif parsed.path == '/config.json':
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

    def _parse_window(self, query):
        """Return (start_ts, end_ts) from the request query string, or None
        after having sent a 400 response."""
        params = urllib.parse.parse_qs(query)
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

    def _send_busy(self, limit, what):
        self.send_response(503, 'Busy')
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Retry-After', str(RETRY_AFTER_SEC))
        body = f'{limit} {what} already running; retry shortly\n'.encode('utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_archive(self, query):
        window = self._parse_window(query)
        if window is None:
            return
        start_ts, end_ts = window

        if end_ts - start_ts > ARCHIVE_MAX_SEC:
            self.send_error(400, 'requested range exceeds 24-hour maximum')
            return

        if not _archive_slots.acquire(blocking=False):
            self._send_busy(ARCHIVE_MAX_CONCURRENT, '/archive downloads')
            return
        # /archive never encodes; its cost IS the disk read + network
        # stream, so the slot is held for the whole response.  (Contrast
        # /video below, which frees its slot before streaming.)
        with _holding(_archive_slots):
            tmp = stage_segments(ARCHIVE_DIR, ARCHIVE_LIVE_DIR,
                                 start_ts, end_ts)
            try:
                # The stored-zip layout is a pure function of the entry
                # names and sizes, so the exact Content-Length is known
                # before any data moves — the zip streams straight to the
                # socket and never touches the disk.
                entries = zip_entries(tmp.name)
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition',
                                 'attachment; filename="archive.zip"')
                self.send_header('Content-Length', str(zip_stream_size(entries)))
                self.end_headers()
                try:
                    write_zip_stream(entries, self.wfile)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # client went away mid-download
            finally:
                tmp.cleanup()

    def _handle_video(self, query):
        window = self._parse_window(query)
        if window is None:
            return
        start_ts, end_ts = window

        if end_ts - start_ts > VIDEO_MAX_SEC:
            self.send_error(400, 'requested range exceeds 12-hour maximum')
            return

        if not _video_slots.acquire(blocking=False):
            self._send_busy(VIDEO_MAX_CONCURRENT, '/video transcodes')
            return
        stage_tmp   = None
        output_tmp  = tempfile.TemporaryDirectory(prefix='video_out_')
        output_path = os.path.join(output_tmp.name, 'video.mp4')
        try:
            # The slot covers only the expensive part (staging + ffmpeg
            # encode).  Streaming the finished file to the client is
            # I/O-bound and must not count against the CPU budget — a slow
            # download would otherwise hold a transcode slot for its whole
            # duration.
            with _holding(_video_slots):
                try:
                    stage_tmp = stage_segments(ARCHIVE_DIR, ARCHIVE_LIVE_DIR,
                                               start_ts, end_ts)
                    transcode_to_video(
                        stage_tmp.name, start_ts, end_ts,
                        VIDEO_FILL_COLOR, output_path,
                        default_width=VIDEO_DEFAULT_WIDTH,
                        default_height=VIDEO_DEFAULT_HEIGHT,
                    )
                except Exception as exc:
                    print(f'[video] transcode error: {exc}', flush=True)
                    self.send_error(500, 'Internal server error')
                    return

            try:
                video_size = os.path.getsize(output_path)
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Disposition', 'attachment; filename="video.mp4"')
                self.send_header('Content-Length', str(video_size))
                self.end_headers()
                with open(output_path, 'rb') as fh:
                    shutil.copyfileobj(fh, self.wfile, length=64 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away mid-download; nothing to salvage
        finally:
            if stage_tmp is not None:
                stage_tmp.cleanup()
            output_tmp.cleanup()

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


if __name__ == '__main__':
    # No request can be in flight yet, so every stage dir in the archive
    # is debris from a previous run's unclean stop — sweep regardless of
    # age (the in-request sweep only removes dirs older than a day).
    sweep_stage_dirs(ARCHIVE_DIR, older_than_sec=0)
    port = int(os.environ.get('WEB_PORT', '8080'))
    server = ThreadingHTTPServer(('', port), Router)
    server.serve_forever()
