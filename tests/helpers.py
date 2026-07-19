"""Shared builders for fake archive segment files.

The timestamped-filename format these produce
({prefix}_YYYYMMDD-HHMMSS.SSS_to_YYYYMMDD-HHMMSS.SSS.mp4) is
load-bearing for both the staging tests and the /video timeline tests —
keep it in one place so it can't drift from archive_times._SEG_RE.
"""
import datetime
import os
import time


def make_live_segment(directory, index, content=b'fake-mp4', age_seconds=0):
    """Write a sequential (active/orphan) live .mp4 file; returns its path."""
    os.makedirs(str(directory), exist_ok=True)
    path = os.path.join(str(directory), f'stream-{index:05d}.mp4')
    with open(path, 'wb') as fh:
        fh.write(content)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def make_completed_segment(directory, start_epoch, end_epoch,
                           content=b'fake-mp4', prefix='stream'):
    """Write a finalized .mp4 with timestamps embedded in its name;
    returns (path, basename)."""
    os.makedirs(str(directory), exist_ok=True)
    utc = datetime.timezone.utc
    fmt = '%Y%m%d-%H%M%S'

    def _fmt(epoch):
        d = datetime.datetime.fromtimestamp(epoch, tz=utc)
        return f'{d.strftime(fmt)}.{d.microsecond // 1000:03d}'

    name = f'{prefix}_{_fmt(start_epoch)}_to_{_fmt(end_epoch)}.mp4'
    path = os.path.join(str(directory), name)
    with open(path, 'wb') as fh:
        fh.write(content)
    return path, name
