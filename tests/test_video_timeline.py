"""
Unit tests for _build_timeline() in video_transcode.py.

No ffmpeg, Docker, or live pipelines required.  _query_file_info is mocked
to return a fixed (duration_s, width, height, fps_str) tuple.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
from video_transcode import _build_timeline, TimelineItem  # noqa: E402

SEGMENT_SEC = 600
DURATION_S  = float(SEGMENT_SEC)
WIDTH, HEIGHT = 1920, 1080
FPS_STR = '25/1'


def _mock_info(path):
    """Always report a full 600-second segment."""
    return DURATION_S, WIDTH, HEIGHT, FPS_STR


def _make_seg(directory, index, age_seconds=0):
    """Write a fake unnamed segment and set its mtime."""
    path = os.path.join(str(directory), f'stream-{index:05d}.mkv')
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

    name = f'stream_{_fmt(start_epoch)}_to_{_fmt(end_epoch)}.mkv'
    path = os.path.join(str(directory), name)
    with open(path, 'wb') as fh:
        fh.write(b'fake')
    return path, name


def _total_duration(timeline):
    return sum(item.duration_s for item in timeline)


# ── empty stage dir ───────────────────────────────────────────────────────────

class TestEmptyStageDir:

    def test_single_gap_covering_full_range(self, tmp_path):
        now = time.time()
        tl = _build_timeline(str(tmp_path), now - 300, now, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert len(tl) == 1
        assert tl[0].path is None

    def test_gap_duration_matches_request(self, tmp_path):
        now = time.time()
        start, end = now - 300, now
        tl = _build_timeline(str(tmp_path), start, end, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert abs(_total_duration(tl) - (end - start)) < 0.001


# ── single segment fills the range ───────────────────────────────────────────

class TestSingleSegmentFillsRange:

    def test_no_gaps_when_segment_spans_request(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 700, now - 100)
        tl = _build_timeline(str(tmp_path), now - 600, now - 200, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert all(item.path is not None for item in tl)

    def test_single_item(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 700, now - 100)
        tl = _build_timeline(str(tmp_path), now - 600, now - 200, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert len(tl) == 1


# ── gaps at the edges ─────────────────────────────────────────────────────────

class TestGapsAtEdges:

    def test_gap_before_first_segment(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 400, now - 400 + SEGMENT_SEC)
        req_start = now - 600  # 200 s before segment starts
        req_end   = now - 100
        tl = _build_timeline(str(tmp_path), req_start, req_end, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert tl[0].path is None, 'first item should be a gap'
        assert tl[-1].path is not None, 'last item should be a file clip'

    def test_gap_after_last_segment(self, tmp_path):
        now = time.time()
        seg_start = now - 900
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_end = now  # 300 s after segment ends
        tl = _build_timeline(str(tmp_path), seg_start, req_end, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert tl[-1].path is None, 'last item should be a trailing gap'
        assert tl[0].path is not None, 'first item should be a file clip'

    def test_total_duration_matches_request_with_leading_gap(self, tmp_path):
        now = time.time()
        seg_start = now - 400
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start, req_end = now - 600, now - 100
        tl = _build_timeline(str(tmp_path), req_start, req_end, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert abs(_total_duration(tl) - (req_end - req_start)) < 0.1


# ── gap in the middle ─────────────────────────────────────────────────────────

class TestGapBetweenSegments:

    def test_middle_gap_present(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 1400, now - 1400 + SEGMENT_SEC)
        _make_renamed(tmp_path, now - 600,  now - 600  + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 1400, now, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert any(item.path is None for item in tl)

    def test_order_is_clip_gap_clip(self, tmp_path):
        now = time.time()
        _make_renamed(tmp_path, now - 1400, now - 1400 + SEGMENT_SEC)
        _make_renamed(tmp_path, now - 600,  now - 600  + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 1400, now - 100, SEGMENT_SEC,
                             _query_info=_mock_info)
        kinds = ['gap' if item.path is None else 'clip' for item in tl]
        assert kinds[0] == 'clip'
        assert 'gap' in kinds
        assert kinds[-1] == 'clip'


# ── clip offset and truncation ────────────────────────────────────────────────

class TestClipOffsetAndTruncation:

    def test_offset_nonzero_when_request_starts_mid_segment(self, tmp_path):
        now = time.time()
        seg_start = now - 800
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start = seg_start + 200  # 200 s into the segment
        tl = _build_timeline(str(tmp_path), req_start, seg_start + 500,
                             SEGMENT_SEC, _query_info=_mock_info)
        clip = next(item for item in tl if item.path is not None)
        assert clip.offset_s == pytest.approx(200.0, abs=0.001)

    def test_duration_truncated_when_request_ends_before_segment_end(self, tmp_path):
        now = time.time()
        seg_start = now - 800
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_end = seg_start + 300  # ends 300 s into a 600-second segment
        tl = _build_timeline(str(tmp_path), seg_start, req_end,
                             SEGMENT_SEC, _query_info=_mock_info)
        clip = next(item for item in tl if item.path is not None)
        assert clip.duration_s == pytest.approx(300.0, abs=0.001)


# ── unnamed segments (mtime-based start) ─────────────────────────────────────

class TestUnnamedSegmentMtimeStart:

    def test_unnamed_segment_included_in_range(self, tmp_path):
        _make_seg(tmp_path, 0, age_seconds=100)
        now = time.time()
        tl = _build_timeline(str(tmp_path), now - 200, now, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert any(item.path is not None for item in tl)

    def test_unnamed_segment_excluded_when_range_before_it(self, tmp_path):
        _make_seg(tmp_path, 0, age_seconds=0)
        mtime = os.path.getmtime(
            os.path.join(str(tmp_path), 'stream-00000.mkv'))
        req_end = mtime - SEGMENT_SEC - 10
        tl = _build_timeline(str(tmp_path), 0, req_end, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert all(item.path is None for item in tl)


# ── total duration invariant ──────────────────────────────────────────────────

class TestTotalDurationInvariant:

    @pytest.mark.parametrize('n_segs,gap_size', [
        (0, 0),
        (1, 0),
        (2, 200),
        (3, 100),
    ])
    def test_total_equals_request_duration(self, tmp_path, n_segs, gap_size):
        now = time.time()
        window = 1800.0  # 30-minute request
        req_start = now - window
        req_end   = now

        cursor = req_start + 50
        for i in range(n_segs):
            seg_end = cursor + SEGMENT_SEC
            if seg_end >= req_end:
                break
            _make_renamed(tmp_path, cursor, seg_end)
            cursor = seg_end + gap_size

        tl = _build_timeline(str(tmp_path), req_start, req_end, SEGMENT_SEC,
                             _query_info=_mock_info)
        assert abs(_total_duration(tl) - window) < 0.1
