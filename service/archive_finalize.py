"""
archive_finalize.py -- publish completed live segments under timestamped names.

ffmpeg's segment muxer writes sequential files (prefix-%05d.mp4) into
ARCHIVE_LIVE_DIR and appends one CSV line per *completed* segment to a
segment-list file:

    <path>,<start_seconds>,<end_seconds>

where the times are stream time (seconds since the capture started).
SegmentFinalizer tails that CSV and, for each new line, moves the file
into ARCHIVE_DIR under the timestamped name produced by
archive_times.renamed_segment_path().  Wall-clock timestamps are
reconstructed as  epoch_anchor + stream_time  — the capture is realtime,
so stream time tracks wall time from the moment ffmpeg started.

The move is staged via a `.part` suffix in ARCHIVE_DIR so /archive's
`*.mp4` glob never matches a partially-copied file: on cross-fs setups
shutil.move writes bytes into the `.part` name, then an atomic os.rename
publishes them under the final name in one step.  On same-fs setups
shutil.move is itself an os.rename, so the extra rename is effectively
free.

If a move fails (disk full, permissions, ...) the source is left in
ARCHIVE_LIVE_DIR under its sequential name so the recording is not lost;
the operator can recover manually.

Partial CSV lines (ffmpeg mid-write) are left unconsumed and re-read on
the next poll — the tail offset only advances past newline-terminated
lines.
"""
import os
import shutil
import sys

from archive_times import renamed_segment_path


class SegmentFinalizer:
    def __init__(self, list_path, live_dir, archive_dir, prefix,
                 epoch_anchor):
        """
        list_path     the CSV segment list ffmpeg appends to
        live_dir      where ffmpeg writes sequential segments
        archive_dir   destination for timestamp-named segments
        prefix        archive filename prefix (DESKTOP_NAME)
        epoch_anchor  time.time() at the moment ffmpeg was spawned
        """
        self.list_path    = list_path
        self.live_dir     = live_dir
        self.archive_dir  = archive_dir
        self.prefix       = prefix
        self.epoch_anchor = epoch_anchor
        self._offset      = 0

    def poll(self):
        """Process any newly-completed segments.  Returns count published."""
        try:
            with open(self.list_path, 'r') as fh:
                fh.seek(self._offset)
                chunk = fh.read()
        except FileNotFoundError:
            return 0

        published = 0
        consumed  = 0
        for line in chunk.splitlines(keepends=True):
            if not line.endswith('\n'):
                break  # partial line still being written; retry next poll
            consumed += len(line)
            entry = self._parse_line(line.strip())
            if entry is None:
                continue
            self._publish(*entry)
            published += 1
        self._offset += consumed
        return published

    def _parse_line(self, line):
        """Return (basename, start_s, end_s) or None for malformed lines."""
        if not line:
            return None
        parts = line.rsplit(',', 2)
        if len(parts) != 3:
            print(f'[service] WARNING: malformed segment-list line: {line!r}',
                  file=sys.stderr, flush=True)
            return None
        name, start_s, end_s = parts
        try:
            return os.path.basename(name), float(start_s), float(end_s)
        except ValueError:
            print(f'[service] WARNING: malformed segment-list line: {line!r}',
                  file=sys.stderr, flush=True)
            return None

    def _publish(self, basename, start_s, end_s):
        src = os.path.join(self.live_dir, basename)
        start_ns = int((self.epoch_anchor + start_s) * 1e9)
        end_ns   = int((self.epoch_anchor + end_s)   * 1e9)
        dst = renamed_segment_path(src, start_ns, end_ns, self.prefix,
                                   dest_dir=self.archive_dir, ext='.mp4')
        publish_part = dst + '.part'
        try:
            shutil.move(src, publish_part)
            os.rename(publish_part, dst)
        except OSError as exc:
            print(f'[service] WARNING: could not publish {src} as {dst}: '
                  f'{exc}', file=sys.stderr, flush=True)
            try:
                os.unlink(publish_part)
            except FileNotFoundError:
                pass
            return
        print(f'[service] archive: {basename} -> {os.path.basename(dst)}',
              flush=True)


def next_segment_number(live_dir, prefix):
    """First free sequential segment number in live_dir.

    Any prefix-NNNNN.mp4 already present is an orphan from a previous run
    (crash, or the finalizer never saw its CSV line).  Starting numbering
    past them means a supervised ffmpeg restart can never overwrite an
    orphan that an operator might still want to recover.
    """
    highest = -1
    try:
        names = os.listdir(live_dir)
    except FileNotFoundError:
        return 0
    for name in names:
        if not (name.startswith(f'{prefix}-') and name.endswith('.mp4')):
            continue
        stem = name[len(prefix) + 1:-4]
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest + 1
