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
  extends past the last completed segment.  Completed segments are copied
  byte-for-byte (they're already valid mp4 after splitmuxsink rotated them).
  The active segment requires a small remux: mp4mux's in-progress file has
  no parseable moov at the front, so we walk its top-level `mdat` boxes,
  extract the AVCC H.264 NAL units, convert them to Annex-B byte stream,
  and pipe to `ffmpeg -c copy -movflags +faststart` for a faststart mp4.

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

STREAM_FRAMERATE = int(os.environ.get('STREAM_FRAMERATE', '30'))
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


def _iter_active_mdats(src_fd):
    """Yield the body bytes of each `mdat` box in an in-progress mp4 file.

    mp4mux writes fragmented MP4 as `ftyp + (moof + mdat)*`, with the
    moov atom landing only at EOS — so an in-progress file has no
    parseable moov, and standard demuxers reject it.  But the actual
    H.264 NAL units sit in the `mdat` boxes verbatim (AVCC-formatted,
    length-prefixed), and we can walk the top-level box structure
    without consulting moov: each box header is 4 bytes of big-endian
    size + 4 bytes of ASCII type, followed by `size - 8` bytes of body.

    Stops cleanly at EOF — the trailing box of an in-progress file may
    be truncated mid-write, and we just yield what we can read.

    The caller owns src_fd; we do not close it.
    """
    offset = 0
    while True:
        os.lseek(src_fd, offset, os.SEEK_SET)
        header = os.read(src_fd, 8)
        if len(header) < 8:
            return
        size = int.from_bytes(header[:4], 'big')
        box_type = header[4:8]

        if size == 1:
            # 64-bit extended size follows the 8-byte header.
            ext = os.read(src_fd, 8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, 'big')
            body_offset = offset + 16
        elif size == 0:
            # "Size 0" means the box extends to EOF — only legal for the
            # final box, and only useful if we know the file size.
            body_offset = offset + 8
            try:
                size = os.fstat(src_fd).st_size - offset
            except OSError:
                return
        else:
            body_offset = offset + 8

        body_size = size - (body_offset - offset)
        if body_size <= 0:
            return

        if box_type == b'mdat':
            os.lseek(src_fd, body_offset, os.SEEK_SET)
            remaining = body_size
            chunks = []
            while remaining > 0:
                chunk = os.read(src_fd, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if chunks:
                yield b''.join(chunks)

        offset += size


def _avcc_to_annex_b(avcc):
    """Convert AVCC-formatted H.264 (length-prefixed NAL units) to Annex-B.

    AVCC:    [4-byte big-endian length][NAL bytes]...
    Annex-B: [0x00 0x00 0x00 0x01][NAL bytes]...

    Annex-B is what `ffmpeg -f h264` expects.  Truncated trailing NALs
    are dropped silently (the in-progress mdat may end mid-NAL).
    """
    out = bytearray()
    i = 0
    n = len(avcc)
    while i + 4 <= n:
        nal_length = int.from_bytes(avcc[i:i + 4], 'big')
        i += 4
        if nal_length == 0 or i + nal_length > n:
            break
        out.extend(b'\x00\x00\x00\x01')
        out.extend(avcc[i:i + nal_length])
        i += nal_length
    return bytes(out)


def _copy_active_to_stage(src_fd, dst):
    """Build a faststart `.mp4` at dst from the in-progress mp4mux file.

    The live container is fragmented MP4 — mp4mux writes the moov atom
    only at EOS, so the in-progress file isn't directly parseable by
    `ffmpeg`/`ffprobe`/browsers (they report "moov atom not found").
    But the actual H.264 stream sits in the `mdat` boxes as AVCC NAL
    units, and we know how to find them without consulting moov.

    Approach:
      1. Walk the top-level boxes of the in-progress file (see
         `_iter_active_mdats`).
      2. For each mdat, convert AVCC NAL units to Annex-B byte stream.
      3. Pipe the byte stream into `ffmpeg -f h264 -c copy
         -movflags +faststart`.  ffmpeg builds a proper mp4 (with moov
         at the front) from the bitstream — h264parse with
         `config-interval=-1` in pipeline.py guarantees SPS/PPS are
         inline before each keyframe, so ffmpeg can synthesize the
         track header from the bitstream itself.

    The remux is `-c copy` (no re-encode), so it costs roughly one
    pass of CPU over the H.264 bytes plus the disk write for `dst`.
    Memory peaks at one mdat-body worth (typically a single GOP of
    video at our fragment-duration=1000ms; ~125 KB to 1 MB depending
    on bitrate).

    Reading via the supplied fd rather than the path means our
    descriptor pins the inode: even if pipeline.py renames the file
    away from live_dir mid-read, we keep reading the same bytes we
    opened.  The caller owns src_fd; we do not close it.

    Raises OSError on remux failure (caller decides whether to retain
    or discard the partial dst).
    """
    cmd = [
        'ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-framerate', str(STREAM_FRAMERATE),
        '-f', 'h264', '-i', 'pipe:0',
        '-c', 'copy', '-movflags', '+faststart',
        dst,
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    bytes_written = 0
    try:
        for mdat_body in _iter_active_mdats(src_fd):
            try:
                annex_b = _avcc_to_annex_b(mdat_body)
                if annex_b:
                    proc.stdin.write(annex_b)
                    bytes_written += len(annex_b)
            except BrokenPipeError:
                # ffmpeg exited (likely an error) before we finished
                # writing — let the wait() below surface the cause.
                break
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
    except Exception:
        proc.kill()
        proc.wait()
        raise
    _, stderr = proc.communicate()
    if proc.returncode != 0 or bytes_written == 0:
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
        tail = stderr.decode('utf-8', errors='replace')[-500:]
        raise OSError(
            f'active-segment remux failed (exit {proc.returncode}, '
            f'{bytes_written} bytes piped): {tail}'
        )


def stage_segments(archive_dir, live_dir, start_ts, end_ts,
                   _copy=_copy_active_to_stage):
    """Copy overlapping segments into a TemporaryDirectory and return it.

    archive_dir   directory of completed .mp4 segments — files with
                  timestamps embedded in their filenames (renamed/moved
                  by pipeline.py when splitmuxsink rotates a fragment).
    live_dir      directory the pipeline writes the in-progress
                  fragmented-MP4 segment into.  On rotation, pipeline.py
                  renames/moves each fragment from live_dir into
                  archive_dir.

    Completed segments have their recording timestamps embedded in their
    filenames; those timestamps are used directly.

    The current active segment (the highest-named sequential file in
    live_dir) is included when its estimated time range overlaps the
    request window.  Its start time is the end of the last completed
    segment; its end time is now().  If no completed segments exist
    before it, the active segment is excluded (no reliable start time
    can be determined).  The live segment is already a fragmented MP4
    on disk, so we stream the bytes through to the stage dir — no remux.

    `_copy` is a seam for unit tests; production callers omit it.

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

    # The active (currently-writing) segment is the highest-named
    # sequential .mp4 in live_dir.  Its start time equals the end of the
    # last completed segment — the same boundary pipeline.py will use
    # when it finalizes the file.  Without a completed predecessor we
    # have no reliable start time, so any other sequential files (orphans
    # from crashed runs) are dropped.
    #
    # Race with segment rollover (pipeline.py renaming into archive_dir):
    #   - If we os.open() the file before the rotation fires, the fd
    #     holds a reference to the inode; we read it and write the bytes
    #     into the stage dir even if the name changes underneath us.
    #   - If the rotation fires before os.open(), we get FileNotFoundError.
    #     The file now exists under its timestamped name in archive_dir,
    #     so we re-glob there to find and copy it as a completed segment.
    live_files = [
        p for p in glob.glob(os.path.join(live_dir, '*.mp4'))
        if parse_segment_times(os.path.basename(p)) is None
    ]
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
                    try:
                        _copy(fd, dst)
                    except OSError as exc:
                        print(f'[web] WARNING: active-segment copy failed: '
                              f'{exc}', flush=True)
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
