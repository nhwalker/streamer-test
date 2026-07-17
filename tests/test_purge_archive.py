"""
Unit tests for purge_archive() in archive_purge.py.

No Docker required — these manipulate real files in
a temporary directory and verify deletion behaviour directly.
"""
import os
import time


from archive_purge import purge_archive


def _make_segment(directory, index, size=1024, age_seconds=0):
    """Create a fake stream-NNNNN.mp4 file with a specific mtime."""
    path = os.path.join(directory, f'stream-{index:05d}.mp4')
    with open(path, 'wb') as fh:
        fh.write(b'\x00' * size)
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
    return path


class TestPurgeArchive:

    def test_no_limits_deletes_nothing(self, tmp_path):
        paths = [_make_segment(tmp_path, i, age_seconds=999999) for i in range(5)]
        purge_archive(str(tmp_path), max_bytes=0, max_age_days=0)
        for p in paths:
            assert os.path.exists(p)

    def test_single_segment_never_touched(self, tmp_path):
        """Only one segment exists — it is the active file, must not be deleted."""
        p = _make_segment(tmp_path, 0, size=1, age_seconds=999999)
        purge_archive(str(tmp_path), max_bytes=1, max_age_days=0)
        assert os.path.exists(p)

    def test_active_file_never_deleted_by_age(self, tmp_path):
        old = _make_segment(tmp_path, 0, age_seconds=999999)
        active = _make_segment(tmp_path, 1, age_seconds=999999)
        purge_archive(str(tmp_path), max_bytes=0, max_age_days=1)
        assert not os.path.exists(old)
        assert os.path.exists(active)

    def test_active_file_never_deleted_by_size(self, tmp_path):
        _make_segment(tmp_path, 0, size=500)
        active = _make_segment(tmp_path, 1, size=500)
        # max_bytes=1 forces deletion of everything it can touch
        purge_archive(str(tmp_path), max_bytes=1, max_age_days=0)
        assert os.path.exists(active)

    def test_age_purge_removes_old_keeps_recent(self, tmp_path):
        old = _make_segment(tmp_path, 0, age_seconds=3 * 86400)   # 3 days old
        recent = _make_segment(tmp_path, 1, age_seconds=3600)      # 1 hour old
        active = _make_segment(tmp_path, 2, age_seconds=60)

        purge_archive(str(tmp_path), max_bytes=0, max_age_days=2)

        assert not os.path.exists(old)
        assert os.path.exists(recent)
        assert os.path.exists(active)

    def test_size_purge_removes_oldest_first(self, tmp_path):
        seg0 = _make_segment(tmp_path, 0, size=400)
        seg1 = _make_segment(tmp_path, 1, size=400)
        active = _make_segment(tmp_path, 2, size=400)

        # Total of purgeable (seg0+seg1) = 800; limit to 500 → delete seg0
        purge_archive(str(tmp_path), max_bytes=500, max_age_days=0)

        assert not os.path.exists(seg0)
        assert os.path.exists(seg1)
        assert os.path.exists(active)

    def test_size_purge_deletes_multiple_to_fit(self, tmp_path):
        segs = [_make_segment(tmp_path, i, size=300) for i in range(4)]
        active = segs[-1]
        purgeable = segs[:-1]

        # purgeable total = 900; limit = 250 → need to delete until ≤ 250
        purge_archive(str(tmp_path), max_bytes=250, max_age_days=0)

        surviving_purgeable = [p for p in purgeable if os.path.exists(p)]
        surviving_size = sum(os.path.getsize(p) for p in surviving_purgeable)
        assert surviving_size <= 250
        assert os.path.exists(active)

    def test_both_limits_age_then_size(self, tmp_path):
        old_large = _make_segment(tmp_path, 0, size=500, age_seconds=5 * 86400)
        new_large = _make_segment(tmp_path, 1, size=500, age_seconds=3600)
        active = _make_segment(tmp_path, 2, size=100)

        # age pass removes old_large; size pass then checks new_large (500) vs limit 200
        purge_archive(str(tmp_path), max_bytes=200, max_age_days=3)

        assert not os.path.exists(old_large)
        assert not os.path.exists(new_large)
        assert os.path.exists(active)

    def test_empty_directory_does_not_crash(self, tmp_path):
        purge_archive(str(tmp_path), max_bytes=1, max_age_days=1)

    def test_exactly_at_size_limit_nothing_deleted(self, tmp_path):
        seg0 = _make_segment(tmp_path, 0, size=512)
        active = _make_segment(tmp_path, 1, size=512)
        purge_archive(str(tmp_path), max_bytes=512, max_age_days=0)
        assert os.path.exists(seg0)
        assert os.path.exists(active)
