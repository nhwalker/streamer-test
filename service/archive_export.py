"""
archive_export.py -- assemble archive segments for download requests.

Backs the /archive and /video endpoints (web_server.py): collects the
completed .mp4 segments that overlap a requested time window into a staging
directory, includes the in-progress segment when it overlaps, and zips the
staged files for /archive.  The /video path hands the same staging directory
to video_transcode.transcode_to_video instead.

Completed segments are copied byte-for-byte (they are valid MP4 after
rotation).  The active segment is also a plain byte-copy: ffmpeg writes the
archive as fragmented MP4 with the moov atom up front (movflags empty_moov)
and per-packet flushing, so the in-progress file is parseable mid-write — a
truncated trailing fragment is simply ignored by players.
"""
import glob
import os
import shutil
import tempfile
import time
import zipfile

from archive_times import parse_segment_times, renamed_segment_path


def _has_complete_moov(head):
    """True when a complete top-level moov box lies within `head`.

    The archive writes moov immediately after ftyp (movflags empty_moov),
    so scanning the first few KB is sufficient.  Walks top-level box
    headers (4-byte big-endian size + 4-byte type).
    """
    off = 0
    while off + 8 <= len(head):
        size = int.from_bytes(head[off:off + 4], 'big')
        if size < 8:
            return False
        if head[off + 4:off + 8] == b'moov':
            return off + size <= len(head)
        off += size
    return False


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
    if not _has_complete_moov(head):
        raise OSError('active segment has no moov yet '
                      '(segment just rotated or nothing flushed)')
    with open(dst, 'wb') as out:
        out.write(head)
        while True:
            chunk = os.read(src_fd, 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


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
