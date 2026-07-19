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

The zip side (zip_entries / zip_stream_size / write_zip_stream) emits
STORED entries — the segments are H.264 video that DEFLATE cannot shrink —
which makes the zip's byte layout a pure function of the entry names and
sizes, so the exact download size is known before any data moves.
"""
import glob
import os
import shutil
import struct
import tempfile
import time
import zlib

from typing import NamedTuple

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
    if not _truncate_to_complete_boxes(dst):
        # ftyp+moov but not one whole fragment yet: with empty_moov the
        # moov holds no samples, so this file contains zero frames and
        # ffmpeg (the /video path) errors out on it.  Skip the active
        # segment; it becomes servable as soon as a fragment lands.
        raise OSError('active segment has no complete fragment yet')


def _truncate_to_complete_boxes(path):
    """Trim a staged fMP4 copy to the last complete moof+mdat pair; return
    True when at least one complete movie fragment survived.

    The live segment is copied mid-write, so the copy can end inside a
    moof/mdat pair.  Browsers ignore a truncated trailing fragment, but
    /video pipes the staged file into ffmpeg, which rejects both a
    mid-box cut and a complete moof whose mdat is missing (the moof
    references sample data that isn't there).  So the walk keeps whole
    top-level boxes only (4-byte big-endian size + type) and then also
    drops a trailing moof left without its mdat.

    Unusual headers (size 0 = "to EOF", size 1 = 64-bit extended) stop
    the walk conservatively: everything from that box on is dropped.
    """
    size = os.path.getsize(path)
    boxes = []          # (type, end_offset) of each complete top-level box
    with open(path, 'rb') as fh:
        off = 0
        while off + 8 <= size:
            fh.seek(off)
            hdr = fh.read(8)
            if len(hdr) < 8:
                break
            box_size = int.from_bytes(hdr[:4], 'big')
            if box_size < 8 or off + box_size > size:
                break
            off += box_size
            boxes.append((hdr[4:8], off))
    while boxes and boxes[-1][0] == b'moof':
        boxes.pop()
    keep = boxes[-1][1] if boxes else 0
    if keep and keep < size:
        os.truncate(path, keep)
    return any(t == b'moof' for t, _ in boxes)


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
    sweep_stage_dirs(archive_dir)
    # The stage dir lives on the archive filesystem so completed segments
    # can be hardlinked instead of copied — the system tmp dir is usually
    # a different filesystem (container layer or tmpfs), where os.link
    # would fail with EXDEV every time.  The dot-prefix keeps it out of
    # every '*.mp4' glob (purge, staging, /archive).
    try:
        tmp = tempfile.TemporaryDirectory(prefix=_STAGE_PREFIX,
                                          dir=archive_dir)
    except OSError:
        tmp = tempfile.TemporaryDirectory(prefix='archive_stage_')
    # mkdtemp creates 0700; open it up to 0755 so host-side tooling that
    # walks a bind-mounted archive volume (find/du, the functional-test
    # harness) can at least descend into a dir it cannot delete.
    os.chmod(tmp.name, 0o755)

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
                _link_or_copy(path, os.path.join(tmp.name, os.path.basename(path)))
            except FileNotFoundError:
                pass  # purge deleted it between glob and link; skip it

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
                                _link_or_copy(path, os.path.join(
                                    tmp.name, os.path.basename(path)))
                            except FileNotFoundError:
                                pass
                        break

    return tmp


# ── Zip streaming ─────────────────────────────────────────────────────────────
#
# Entries are STORED: the segments are already-compressed H.264 video that
# DEFLATE cannot shrink but would burn a core on.  Stored entries also make
# the zip's byte layout a pure function of the entry names and sizes, so
# web_server can send an exact Content-Length and stream the zip straight to
# the socket without ever materialising it on disk.
#
# The writer is hand-rolled rather than zipfile-based because zipfile's
# unseekable-stream mode emits data descriptors (general-purpose flag bit 3),
# and STORED-plus-descriptor zips are rejected by streaming readers such as
# Java's ZipInputStream (a stored entry's length is unknowable mid-stream).
# Writing real sizes and CRCs into the local headers instead costs one extra
# read pass per file (CRC first, then the data), which is cheap next to the
# transfer itself.

_ZIP64_LIMIT = 0xFFFF_FFFF   # from this value on, a field moves to zip64


class ZipEntry(NamedTuple):
    arcname:   str
    path:      str
    size:      int
    date_time: tuple   # (Y, M, D, h, m, s) local time, for the zip headers


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


def _dos_datetime(date_time):
    """(dos_time, dos_date) for a (Y, M, D, h, m, s) tuple; clamps pre-1980."""
    y, mo, d, h, mi, s = date_time[:6]
    if y < 1980:
        y, mo, d, h, mi, s = 1980, 1, 1, 0, 0, 0
    return (h << 11) | (mi << 5) | (s // 2), ((y - 1980) << 9) | (mo << 5) | d


def _entry_data(path, size):
    """Yield exactly `size` bytes of path, zero-filling if it comes up short.

    Staged files are immutable, so a short read means something external
    mutated the stage dir; padding keeps the byte count — and with it the
    HTTP Content-Length framing — valid regardless.
    """
    remaining = size
    with open(path, 'rb') as fh:
        while remaining > 0:
            chunk = fh.read(min(1024 * 1024, remaining))
            if not chunk:
                yield bytes(remaining)
                return
            remaining -= len(chunk)
            yield chunk


def _file_crc(path, size):
    """CRC-32 of exactly the bytes _entry_data will stream for path."""
    crc = 0
    for chunk in _entry_data(path, size):
        crc = zlib.crc32(chunk, crc)
    return crc


def _write_zip(entries, out, include_data):
    """Write a stored zip of `entries` to out (any object with .write).

    With include_data=False no file is opened at all: headers are emitted
    and offsets advanced as if the data were written, which is how
    zip_stream_size computes the exact length without reading a byte.
    """
    def w(data):
        out.write(data)
        return len(data)

    offset  = 0
    central = []
    for e in entries:
        try:
            name  = e.arcname.encode('ascii')
            flags = 0
        except UnicodeEncodeError:
            name  = e.arcname.encode('utf-8')
            flags = 0x0800                      # name is UTF-8
        crc   = _file_crc(e.path, e.size) if include_data else 0
        zip64 = e.size >= _ZIP64_LIMIT
        dtime, ddate = _dos_datetime(e.date_time)
        size_marker  = 0xFFFFFFFF if zip64 else e.size
        extra = (struct.pack('<HHQQ', 0x0001, 16, e.size, e.size)
                 if zip64 else b'')
        header_offset = offset
        offset += w(struct.pack(
            '<IHHHHHIIIHH', 0x04034B50, 45 if zip64 else 20, flags, 0,
            dtime, ddate, crc, size_marker, size_marker,
            len(name), len(extra)))
        offset += w(name)
        offset += w(extra)
        if include_data:
            for chunk in _entry_data(e.path, e.size):
                offset += w(chunk)
        else:
            offset += e.size
        central.append((name, flags, crc, e.size, header_offset,
                        dtime, ddate))

    cd_start  = offset
    n_entries = len(central)
    for name, flags, crc, size, header_offset, dtime, ddate in central:
        extra_fields = []
        size_marker  = size
        if size >= _ZIP64_LIMIT:
            extra_fields += [size, size]        # uncompressed, compressed
            size_marker   = 0xFFFFFFFF
        off_marker = header_offset
        if header_offset >= _ZIP64_LIMIT:
            extra_fields.append(header_offset)
            off_marker = 0xFFFFFFFF
        extra = (struct.pack('<HH' + 'Q' * len(extra_fields),
                             0x0001, 8 * len(extra_fields), *extra_fields)
                 if extra_fields else b'')
        version = 45 if extra_fields else 20
        offset += w(struct.pack(
            '<IHHHHHHIIIHHHHHII', 0x02014B50, version, version, flags, 0,
            dtime, ddate, crc, size_marker, size_marker,
            len(name), len(extra), 0, 0, 0, 0, off_marker))
        offset += w(name)
        offset += w(extra)
    cd_size = offset - cd_start

    need_zip64_eocd = (n_entries >= 0xFFFF or cd_size >= _ZIP64_LIMIT
                       or cd_start >= _ZIP64_LIMIT)
    eocd_entries = 0xFFFF     if need_zip64_eocd else n_entries
    eocd_cd_size = 0xFFFFFFFF if need_zip64_eocd else cd_size
    eocd_cd_off  = 0xFFFFFFFF if need_zip64_eocd else cd_start
    if need_zip64_eocd:
        offset += w(struct.pack(
            '<IQHHIIQQQQ', 0x06064B50, 44, 45, 45, 0, 0,
            n_entries, n_entries, cd_size, cd_start))
        offset += w(struct.pack('<IIQI', 0x07064B50, 0,
                                cd_start + cd_size, 1))
    offset += w(struct.pack(
        '<IHHHHIIH', 0x06054B50, 0, 0, eocd_entries, eocd_entries,
        eocd_cd_size, eocd_cd_off, 0))
    return offset


class _NullWriter:
    def write(self, data):
        pass


def zip_stream_size(entries):
    """Exact byte length write_zip_stream will produce for `entries`.

    Costs only header arithmetic — no file is opened or read.  (The size
    comes from _write_zip's offset accounting, which advances past the
    file data even when none is written.)
    """
    return _write_zip(entries, _NullWriter(), include_data=False)


def write_zip_stream(entries, out):
    """Stream a stored zip of `entries` to out (any object with .write)."""
    _write_zip(entries, out, include_data=True)


def zip_segments(stage_dir, zip_path):
    """Write all .mp4 files in stage_dir into a zip archive at zip_path."""
    with open(zip_path, 'wb') as fh:
        write_zip_stream(zip_entries(stage_dir), fh)
