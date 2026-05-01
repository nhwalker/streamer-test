"""archive_purge.py -- retention logic for rotating .mkv archive segments."""
import glob
import os
import time


def purge_archive(archive_dir, max_bytes, max_age_days):
    """Delete old segments to enforce size and age retention limits.

    The most recently created segment is always kept (it may still be open
    for writing by splitmuxsink).  Remaining segments are evaluated oldest
    first: age-expired ones are removed unconditionally, then the oldest are
    removed until the total size of surviving segments is within max_bytes.
    """
    segments = sorted(
        glob.glob(os.path.join(archive_dir, '*.mkv')),
        key=os.path.getmtime,
    )
    if len(segments) < 2:
        return  # nothing to purge (active file is always kept)

    # The last segment (highest mtime) is the one splitmuxsink may be writing.
    purgeable = segments[:-1]

    if max_age_days:
        cutoff = time.time() - max_age_days * 86400
        surviving = []
        for path in purgeable:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                print(f'[service] purge: deleted {path} (age)', flush=True)
            else:
                surviving.append(path)
        purgeable = surviving

    if max_bytes:
        total = sum(os.path.getsize(p) for p in purgeable)
        for path in purgeable:
            if total <= max_bytes:
                break
            size = os.path.getsize(path)
            os.remove(path)
            total -= size
            print(f'[service] purge: deleted {path} (size)', flush=True)
