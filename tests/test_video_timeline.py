"""
Unit tests for _build_timeline() in video_transcode.py.

No ffmpeg, Docker, or live pipelines required.

Model: _build_timeline returns a list of TimelineItem (one per overlapping
segment, no gap items).  Gaps are implicit — the base color video fills them
in transcode_to_video.

All files in the stage directory must have timestamps in their filenames.
Files without recognized timestamps are skipped.

Each item has:
  path           – file path
  offset_s       – seconds to skip from the start of the file (> 0 when the
                   segment starts before start_ts)
  output_start_s – seconds from the start of the output where this clip begins
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
from video_transcode import _build_timeline  # noqa: E402

SEGMENT_SEC = 600


@pytest.fixture(autouse=True)
def _mock_video_file_info(monkeypatch):
    """Stub out ffprobe so unit tests work without ffprobe installed.

    Returns valid dimensions for every path so that synthetic test files
    (containing b'fake') are included in the timeline rather than filtered.
    """
    import video_transcode as _vt
    monkeypatch.setattr(_vt, '_query_file_info', lambda _path: (600.0, 1280, 720, '25/1'))


def _make_seg(directory, index, age_seconds=0):
    """Write a fake unnamed segment (no timestamp in name) and set its mtime."""
    path = os.path.join(str(directory), f'stream-{index:05d}.mp4')
    with open(path, 'wb') as fh:
        fh.write(b'fake')
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _make_renamed(directory, start_epoch, end_epoch):
    """Write a fake renamed segment (timestamps in filename)."""
    import datetime as _dt
    utc = _dt.timezone.utc
    fmt = '%Y%m%d-%H%M%S'

    def _fmt(e):
        d = _dt.datetime.fromtimestamp(e, tz=utc)
        return f'{d.strftime(fmt)}.{d.microsecond // 1000:03d}'

    name = f'stream_{_fmt(start_epoch)}_to_{_fmt(end_epoch)}.mp4'
    path = os.path.join(str(directory), name)
    with open(path, 'wb') as fh:
        fh.write(b'fake')
    return path, name


# ── empty stage dir ───────────────────────────────────────────────────────────

class TestEmptyStageDir:

    def test_returns_empty_list(self, tmp_path):
        now = time.time()
        tl = _build_timeline(str(tmp_path), now - 300, now)
        assert tl == []

    def test_returns_empty_list_any_range(self, tmp_path):
        now = time.time()
        tl = _build_timeline(str(tmp_path), now - 1800, now)
        assert tl == []


# ── single segment fills the range ───────────────────────────────────────────

class TestSingleSegmentFillsRange:

    def test_one_item_returned(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 700, now - 100)
        tl = _build_timeline(str(tmp_path), now - 600, now - 200)
        assert len(tl) == 1

    def test_offset_nonzero_when_segment_starts_before_window(self, tmp_path):
        now = time.time()
        seg_start = now - 700
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start = now - 600  # 100 s into the segment
        tl = _build_timeline(str(tmp_path), req_start, now - 200)
        assert tl[0].offset_s == pytest.approx(100.0, abs=0.01)
        assert tl[0].output_start_s == pytest.approx(0.0, abs=0.001)

    def test_zero_offset_zero_output_start_when_segment_starts_at_window(self, tmp_path):
        now = time.time()
        seg_start = now - 600
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), seg_start, now - 100)
        assert tl[0].offset_s == pytest.approx(0.0, abs=0.001)
        assert tl[0].output_start_s == pytest.approx(0.0, abs=0.001)


# ── gaps at the edges ─────────────────────────────────────────────────────────

class TestGapsAtEdges:

    def test_one_item_with_leading_gap(self, tmp_path):
        now = time.time()
        seg_start = now - 400
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 600, now - 100)
        assert len(tl) == 1
        assert tl[0].path is not None

    def test_output_start_nonzero_with_leading_gap(self, tmp_path):
        now = time.time()
        seg_start = now - 400
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start = now - 600  # 200 s before segment
        tl = _build_timeline(str(tmp_path), req_start, now - 100)
        assert tl[0].output_start_s == pytest.approx(200.0, abs=0.1)

    def test_one_item_with_trailing_gap(self, tmp_path):
        now = time.time()
        seg_start = now - 900
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), seg_start, now)
        assert len(tl) == 1
        assert tl[0].path is not None

    def test_zero_offset_with_trailing_gap(self, tmp_path):
        now = time.time()
        seg_start = now - 900
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), seg_start, now)
        assert tl[0].offset_s == pytest.approx(0.0, abs=0.001)
        assert tl[0].output_start_s == pytest.approx(0.0, abs=0.001)


# ── gap in the middle ─────────────────────────────────────────────────────────

class TestGapBetweenSegments:

    def test_two_items_with_middle_gap(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 1400, now - 1400 + SEGMENT_SEC)
        _make_renamed(tmp_path, now - 600,  now - 600  + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 1400, now)
        assert len(tl) == 2

    def test_second_item_output_start_nonzero(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 1400, now - 1400 + SEGMENT_SEC)
        _make_renamed(tmp_path, now - 600,  now - 600  + SEGMENT_SEC)
        req_start = now - 1400
        tl = _build_timeline(str(tmp_path), req_start, now - 100)
        assert tl[0].output_start_s == pytest.approx(0.0, abs=0.001)
        # second segment starts 800 s after req_start
        assert tl[1].output_start_s == pytest.approx(800.0, abs=0.1)

    def test_items_ordered_by_start_time(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 1400, now - 1400 + SEGMENT_SEC)
        _make_renamed(tmp_path, now - 600,  now - 600  + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 1400, now - 100)
        assert tl[0].output_start_s < tl[1].output_start_s


# ── clip offset ───────────────────────────────────────────────────────────────

class TestClipOffset:

    def test_offset_nonzero_when_request_starts_mid_segment(self, tmp_path):
        now = time.time()
        seg_start = now - 800
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start = seg_start + 200  # 200 s into the segment
        tl = _build_timeline(str(tmp_path), req_start, seg_start + 500)
        assert tl[0].offset_s == pytest.approx(200.0, abs=0.001)

    def test_output_start_zero_when_request_starts_mid_segment(self, tmp_path):
        now = time.time()
        seg_start = now - 800
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start = seg_start + 200
        tl = _build_timeline(str(tmp_path), req_start, seg_start + 500)
        assert tl[0].output_start_s == pytest.approx(0.0, abs=0.001)

    def test_zero_offset_when_segment_starts_within_window(self, tmp_path):
        now = time.time()
        seg_start = now - 400
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 600, now)
        assert tl[0].offset_s == pytest.approx(0.0, abs=0.001)


# ── files without timestamps are ignored ─────────────────────────────────────

class TestUnnamedFilesIgnored:

    def test_unnamed_file_not_included(self, tmp_path):
        """Files without timestamp in name are skipped."""
        _make_seg(tmp_path, 0, age_seconds=100)
        now = time.time()
        tl = _build_timeline(str(tmp_path), now - 200, now)
        assert tl == []

    def test_mix_named_and_unnamed(self, tmp_path):
        """Unnamed files are skipped; renamed files are included normally."""
        now = time.time()
        _make_seg(tmp_path, 0, age_seconds=100)
        _make_renamed(tmp_path, now - 200, now - 200 + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 200, now)
        assert len(tl) == 1
