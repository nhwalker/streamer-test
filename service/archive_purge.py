"""archive_purge.py -- retention logic for rotating .mp4 archive segments."""
import glob
import os
import time


def purge_archive(archive_dir, max_bytes, max_age_days):
    """Delete old segments to enforce size and age retention limits.

    The most recently finalized segment is always kept as a safety margin.
    Remaining segments are evaluated oldest first: age-expired ones are
    removed unconditionally, then the oldest are removed until the total
    size of surviving segments is within max_bytes.

    splitmuxsink writes the in-progress fragmented .mp4 to ARCHIVE_LIVE_DIR
    (a different directory), so the live fragment is never visible here.
    """
    segments = sorted(
        glob.glob(os.path.join(archive_dir, '*.mp4')),
        key=os.path.getmtime,
    )
    if len(segments) < 2:
        return  # nothing to purge (newest is kept as a safety margin)

    # Keep the most recently finalized segment as a safety margin.
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
