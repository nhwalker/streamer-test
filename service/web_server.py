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
  Returns a zip of the .mp4 segments whose recorded time overlaps the requested
  window.  The active (currently-writing) segment is included when the window
  extends past the last completed segment; it is still being written as
  Matroska, so it is remuxed on the fly into a faststart .mp4 with `ffmpeg
  -c copy` (no re-encoding) before being added to the zip.

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
                       segments (timestamp-named, faststart)
  ARCHIVE_LIVE_DIR     Directory the in-progress .mkv   (/archive-live)
                       segment is being written into
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
import subprocess
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
ARCHIVE_LIVE_DIR     = os.environ.get('ARCHIVE_LIVE_DIR', '/archive-live')
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


def _remux_active_to_mp4(src_fd, dst):
    """Remux a live Matroska fragment (read from an open fd) to faststart MP4.

    Reading via /proc/self/fd/<n> rather than the path means our open file
    descriptor pins the inode: even if pipeline.py moves the .mkv away from
    live_dir mid-read, ffmpeg keeps reading the same bytes we opened.  The
    `+faststart` flag rewrites the moov atom to the front of the output so
    web players can begin decoding from the first received bytes.
    `-c copy` means no re-encoding — the H.264 packets are remuxed verbatim.
    `-err_detect ignore_err` tolerates a fragment whose tail is a partially
    written cluster (the live file may still be being flushed at open time).
    """
    cmd = [
        'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-fflags', '+genpts', '-err_detect', 'ignore_err',
        '-i', f'/proc/self/fd/{src_fd}',
        '-c', 'copy', '-map', '0:v:0',
        '-movflags', '+faststart',
        dst,
    ]
    return subprocess.run(cmd, capture_output=True, pass_fds=(src_fd,))


def stage_segments(archive_dir, live_dir, start_ts, end_ts,
                   _remux=_remux_active_to_mp4):
    """Copy overlapping segments into a TemporaryDirectory and return it.

    archive_dir   directory of completed .mp4 segments — files with
                  timestamps embedded in their filenames (finalized by
                  pipeline.py when splitmuxsink rotates a fragment, after
                  remux from .mkv to faststart .mp4).
    live_dir      directory the pipeline writes the in-progress .mkv
                  segment into.  Pipeline remuxes each fragment from
                  live_dir into archive_dir on rotation.

    Completed segments have their recording timestamps embedded in their
    filenames; those timestamps are used directly.

    The current active segment (the highest-named unnamed file in live_dir)
    is included when its estimated time range overlaps the request window.
    Its start time is the end of the last completed segment; its end time
    is now().  If no completed segments exist before it, the active
    segment is excluded (no reliable start time can be determined).
    Because the live segment is still Matroska, it is remuxed on the fly
    into a faststart .mp4 (no re-encoding) before being staged.

    `_remux` is a seam for unit tests — pass a stub to avoid invoking
    ffmpeg.  Production callers omit it.

    The caller owns the returned TemporaryDirectory and must clean it up
    (use as a context manager or call .cleanup() explicitly).
    """
    tmp = tempfile.TemporaryDirectory(prefix='archive_stage_')

    now = time.time()
    renamed = []
    for path in glob.glob(os.path.join(archive_dir, '*.mp4')):
        times = parse_segment_times(os.path.basename(path))
        if times:
            renamed.append((path, times[0], times[1]))

    renamed.sort(key=lambda x: x[1])

    for path, seg_start, seg_end in renamed:
        if seg_start < end_ts and seg_end > start_ts:
            try:
                shutil.copy2(path, os.path.join(tmp.name, os.path.basename(path)))
            except FileNotFoundError:
                pass  # purge deleted it between glob and copy; skip it

    # The active (currently-writing) segment is the highest-named .mkv in
    # live_dir.  Its start time equals the end of the last completed
    # segment — the same boundary pipeline.py will use when it finalizes
    # the file.  Without a completed predecessor we have no reliable start
    # time, so any other unnamed files (orphans from crashed runs) are
    # dropped.
    #
    # Race with segment rollover (pipeline.py finalizing into archive_dir):
    #   - If we os.open() the file before the rotation fires, the fd holds
    #     a reference to the inode; ffmpeg reads it via /proc/self/fd/N
    #     and the remux completes even if the name changes underneath us.
    #   - If the rotation fires before os.open(), we get FileNotFoundError.
    #     The file now exists as a faststart .mp4 in archive_dir, so we
    #     re-glob there to find and copy it as a completed segment.
    live_files = glob.glob(os.path.join(live_dir, '*.mkv'))
    if live_files and renamed:
        active    = max(live_files)
        seg_start = renamed[-1][2]
        seg_end   = now
        if seg_start < end_ts and seg_end > start_ts and seg_end - seg_start >= 1.0:
            prefix = os.path.basename(active).rsplit('-', 1)[0]
            dst = renamed_segment_path(
                active,
                int(seg_start * 1e9),
                int(seg_end   * 1e9),
                prefix,
                dest_dir=tmp.name,
                ext='.mp4',
            )
            fd = None
            try:
                fd = os.open(active, os.O_RDONLY)
            except FileNotFoundError:
                fd = None

            if fd is not None:
                try:
                    result = _remux(fd, dst)
                    if result.returncode != 0:
                        stderr = result.stderr.decode('utf-8', errors='replace')[-500:]
                        print(f'[web] WARNING: active-segment remux failed '
                              f'(exit {result.returncode}): {stderr}', flush=True)
                        try:
                            os.unlink(dst)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(fd)
            else:
                # Rollover fired in the window before os.open().
                # Find the now-completed .mp4 by its start timestamp.
                for path in glob.glob(os.path.join(archive_dir, '*.mp4')):
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
    """Write all .mp4 files in stage_dir into a zip archive at zip_path."""
    with zipfile.ZipFile(zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(glob.glob(os.path.join(stage_dir, '*.mp4'))):
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
