"""
archive_export.py -- assemble archive segments for download requests.

Backs the /archive and /video endpoints (web_server.py): collects the
completed .mp4 segments that overlap a requested time window into a staging
directory, includes the in-progress segment when it overlaps, and streams a
zip of the staged files for /archive.  The /video path hands the same
staging directory to video_transcode.transcode_to_video instead.

Completed segments are hardlinked into the stage dir (they are valid,
immutable MP4 after rotation, and the link pins the inode against the
purger without moving any bytes; a byte copy is the fallback when linking
is impossible).  The active segment is a plain byte-copy: ffmpeg writes
the archive as fragmented MP4 with the moov atom up front (movflags
empty_moov) and per-packet flushing, so the in-progress file is parseable
mid-write — a truncated trailing fragment is simply ignored by players.

The zip itself is produced by zip_stream.py (stored entries, exact size
known before any data moves); zip_entries() here maps the staged files
onto its ZipEntry model.
"""
import glob
import os
import shutil
import tempfile
import time

from typing import NamedTuple

from archive_times import parse_segment_times, renamed_segment_path
from fmp4 import has_complete_moov, truncate_to_complete_boxes
from zip_stream import ZipEntry, write_zip_stream


def _copy_active_to_stage(src_fd, dst):
    """Byte-copy the in-progress archive segment to dst.

    The live segment is fragmented MP4 written with
    `movflags=+frag_keyframe+empty_moov` and per-packet flushing — the
    moov atom sits at the front of the on-disk file from the moment the
    segment opens, so the in-progress file is directly parseable by
    browsers/ffprobe.  A trailing fragment that is truncated mid-write is
    simply ignored by players, so a plain copy of whatever bytes exist
    right now is a valid, playable MP4.

    The moov check guards the race right at segment rotation (and any
    residual write buffering): a copy without a complete moov would make
    downstream ffmpeg fail with "moov atom not found", so such a file is
    rejected here and the caller skips the active segment instead of
    failing the whole request.

    Reading via the supplied fd rather than the path means our
    descriptor pins the inode: even if the finalizer renames the file
    away from live_dir mid-read, we keep reading the same bytes we
    opened.  The caller owns src_fd; we do not close it.

    Raises OSError when the file carries no parseable video yet.
    """
    os.lseek(src_fd, 0, os.SEEK_SET)
    head = b''
    while len(head) < 64 * 1024:
        chunk = os.read(src_fd, 64 * 1024 - len(head))
        if not chunk:
            break
        head += chunk
    if not has_complete_moov(head):
        raise OSError('active segment has no moov yet '
                      '(segment just rotated or nothing flushed)')
    with open(dst, 'wb') as out:
        out.write(head)
        while True:
            chunk = os.read(src_fd, 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    if not truncate_to_complete_boxes(dst):
        # ftyp+moov but not one whole fragment yet: with empty_moov the
        # moov holds no samples, so this file contains zero frames and
        # ffmpeg (the /video path) errors out on it.  Skip the active
        # segment; it becomes servable as soon as a fragment lands.
        raise OSError('active segment has no complete fragment yet')


_STAGE_PREFIX    = '.archive_stage_'
# A stage dir this old cannot belong to a live request (even a maximum-size
# /video re-encode finishes well inside it); it is debris from a crash.
_STAGE_STALE_SEC = 24 * 3600


def sweep_stage_dirs(archive_dir, older_than_sec=_STAGE_STALE_SEC):
    """Delete stage dirs leaked by interrupted requests.

    Stage dirs live inside archive_dir (see stage_segments) and are
    normally removed by TemporaryDirectory.cleanup(); a process killed
    mid-request leaks one, and because it holds hardlinks it keeps
    purged segments' disk space alive until removed.

    Called with the default age from stage_segments (anything younger
    might belong to a request in flight) and with older_than_sec=0 at
    web-server boot, when no request can be in flight at all.
    """
    cutoff = time.time() - older_than_sec
    for path in glob.glob(os.path.join(archive_dir, _STAGE_PREFIX + '*')):
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def _link_or_copy(src, dst):
    """Hardlink src to dst, falling back to a byte copy.

    A hardlink pins the completed segment's inode against the purger with
    zero data movement.  The fallback covers stage dirs that could not be
    created on the archive filesystem and filesystems without hardlink
    support.  FileNotFoundError (the purger won the race) propagates to
    the caller either way.
    """
    try:
        os.link(src, dst)
    except FileNotFoundError:
        raise
    except OSError:
        shutil.copy2(src, dst)


class _Completed(NamedTuple):
    """A finalized segment: its path and the recording window its
    timestamped filename declares."""
    path:  str
    start: float
    end:   float


def _completed_segments(archive_dir):
    """Every finalized segment in archive_dir, sorted by start time."""
    segments = []
    for path in glob.glob(os.path.join(archive_dir, '*.mp4')):
        times = parse_segment_times(os.path.basename(path))
        if times:
            segments.append(_Completed(path, times[0], times[1]))
    segments.sort(key=lambda s: s.start)
    return segments


def _stage_completed(seg, start_ts, end_ts, stage_dir):
    """Link one completed segment into the stage dir if it overlaps the
    window; a vanished file (the purger won the race) is skipped."""
    if seg.start < end_ts and seg.end > start_ts:
        try:
            _link_or_copy(seg.path,
                          os.path.join(stage_dir, os.path.basename(seg.path)))
        except FileNotFoundError:
            pass


def _make_stage_dir(archive_dir):
    """Create the staging TemporaryDirectory on the archive filesystem.

    It must live there so completed segments can be hardlinked instead of
    copied — the system tmp dir is usually a different filesystem
    (container layer or tmpfs), where os.link would fail with EXDEV every
    time.  The dot-prefix keeps it out of every '*.mp4' glob (purge,
    staging, /archive).  Falls back to the system tmp dir (and therefore
    to byte copies) if the archive dir refuses a subdirectory.
    """
    try:
        tmp = tempfile.TemporaryDirectory(prefix=_STAGE_PREFIX,
                                          dir=archive_dir)
    except OSError:
        tmp = tempfile.TemporaryDirectory(prefix='archive_stage_')
    # mkdtemp creates 0700; open it up to 0755 so host-side tooling that
    # walks a bind-mounted archive volume (find/du, the functional-test
    # harness) can at least descend into a dir it cannot delete.
    os.chmod(tmp.name, 0o755)
    return tmp


def _stage_active(archive_dir, live_dir, start_ts, end_ts,
                  last_completed_end, now, stage_dir, _copy):
    """Stage the in-progress segment when its estimated window overlaps.

    The active segment is the highest-named sequential .mp4 in live_dir.
    Its start time equals the end of the last completed segment — the
    same boundary pipeline.py will use when it finalizes the file — and
    its end time is now().  Other sequential files (orphans from crashed
    runs) are dropped.

    Race with segment rollover (pipeline.py renaming into archive_dir):
      - If we os.open() the file before the rotation fires, the fd holds
        a reference to the inode; we read it and write the bytes into
        the stage dir even if the name changes underneath us.
      - If the rotation fires before os.open(), we get FileNotFoundError.
        The file now exists under its timestamped name in archive_dir,
        so we look it up there by start time and stage it as a completed
        segment instead.
    """
    live_files = [
        p for p in glob.glob(os.path.join(live_dir, '*.mp4'))
        if parse_segment_times(os.path.basename(p)) is None
    ]
    if not live_files:
        return
    active    = max(live_files)
    seg_start = last_completed_end
    seg_end   = now
    if not (seg_start < end_ts and seg_end > start_ts
            and seg_end - seg_start >= 1.0):
        return

    dst = renamed_segment_path(
        active,
        int(seg_start * 1e9),
        int(seg_end   * 1e9),
        os.path.basename(active).rsplit('-', 1)[0],
        dest_dir=stage_dir,
        ext='.mp4',
    )
    try:
        fd = os.open(active, os.O_RDONLY)
    except FileNotFoundError:
        # Rollover fired in the window before os.open(); find the
        # now-completed file by its start timestamp.
        for seg in _completed_segments(archive_dir):
            if abs(seg.start - seg_start) < 1.0:
                _stage_completed(seg, start_ts, end_ts, stage_dir)
                break
        return

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


def stage_segments(archive_dir, live_dir, start_ts, end_ts,
                   _copy=_copy_active_to_stage):
    """Copy overlapping segments into a TemporaryDirectory and return it.

    archive_dir   directory of completed .mp4 segments — files with
                  timestamps embedded in their filenames (renamed/moved
                  by the finalize watcher when ffmpeg rotates a segment).
    live_dir      directory ffmpeg writes the in-progress fragmented-MP4
                  segment into.  On rotation, the finalize watcher
                  renames/moves each segment from live_dir into
                  archive_dir.

    Completed segments have their recording timestamps embedded in their
    filenames; those timestamps are used directly.  The active segment is
    included when its estimated window overlaps (see _stage_active); when
    no completed segment precedes it there is no reliable start estimate,
    so it is excluded.  The live segment is already a fragmented MP4 on
    disk, so its bytes stream through to the stage dir — no remux.

    `_copy` is a seam for unit tests; production callers omit it.

    The caller owns the returned TemporaryDirectory and must clean it up
    (use as a context manager or call .cleanup() explicitly).
    """
    sweep_stage_dirs(archive_dir)
    tmp = _make_stage_dir(archive_dir)
    now = time.time()

    completed = _completed_segments(archive_dir)
    for seg in completed:
        _stage_completed(seg, start_ts, end_ts, tmp.name)

    if completed:
        _stage_active(archive_dir, live_dir, start_ts, end_ts,
                      last_completed_end=completed[-1].end,
                      now=now, stage_dir=tmp.name, _copy=_copy)
    return tmp


# ── Zip assembly ──────────────────────────────────────────────────────────────
#
# The zip format work (stored entries, exact size prediction, zip64) lives
# in zip_stream.py; this side only maps staged segment files onto its
# ZipEntry model.


def zip_entries(stage_dir):
    """One ZipEntry per .mp4 in stage_dir, sorted by name.

    Sizes are captured here, once: staged files are immutable, and every
    later step (zip_stream_size, write_zip_stream) works from this
    snapshot so the streamed bytes always match the announced size.
    """
    entries = []
    for path in sorted(glob.glob(os.path.join(stage_dir, '*.mp4'))):
        st = os.stat(path)
        entries.append(ZipEntry(
            arcname   = os.path.basename(path),
            path      = path,
            size      = st.st_size,
            date_time = time.localtime(st.st_mtime)[:6],
        ))
    return entries


def zip_segments(stage_dir, zip_path):
    """Write all .mp4 files in stage_dir into a zip archive at zip_path."""
    with open(zip_path, 'wb') as fh:
        write_zip_stream(zip_entries(stage_dir), fh)
