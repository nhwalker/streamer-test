"""
Unit tests for _build_timeline() and the concat-based ffmpeg assembly in
video_transcode.py.

No ffmpeg, Docker, or live pipelines required (subprocess.run is stubbed
for the assembly tests).

Model: _build_timeline returns a list of TimelineItem (one per overlapping
segment, no gap items).  transcode_to_video turns that into consecutive
pieces — one clip per segment, one generated color clip per uncovered
stretch — joined with the concat filter.

All files in the stage directory must have timestamps in their filenames.
Files without recognized timestamps are skipped.

Each item has:
  path           – file path
  offset_s       – seconds to skip from the start of the file (> 0 when the
                   segment starts before start_ts)
  output_start_s – seconds from the start of the output where this clip begins
  duration_s     – clip length after clipping to the window
"""
import os
import time

import pytest

import video_transcode
from video_transcode import _build_timeline, transcode_to_video

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


# ── clip duration ─────────────────────────────────────────────────────────────

class TestClipDuration:

    def test_duration_of_segment_fully_inside_window(self, tmp_path):
        now = time.time()
        seg_start = now - 500
        _make_renamed(tmp_path, seg_start, seg_start + 300)
        tl = _build_timeline(str(tmp_path), now - 600, now - 100)
        assert tl[0].duration_s == pytest.approx(300.0, abs=0.01)

    def test_duration_clipped_at_window_end(self, tmp_path):
        now = time.time()
        seg_start = now - 400
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        tl = _build_timeline(str(tmp_path), now - 600, now - 100)
        # clip runs from seg_start to window end: 300 s
        assert tl[0].duration_s == pytest.approx(300.0, abs=0.01)

    def test_duration_reduced_by_head_offset(self, tmp_path):
        now = time.time()
        seg_start = now - 700
        _make_renamed(tmp_path, seg_start, seg_start + SEGMENT_SEC)
        req_start = now - 600  # 100 s into the segment
        tl = _build_timeline(str(tmp_path), req_start, now - 200)
        # clip covers [req_start, seg_end=now-100] ∩ [.., now-200] → 400 s
        assert tl[0].duration_s == pytest.approx(400.0, abs=0.01)


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


# ── concat assembly (transcode_to_video's ffmpeg command) ─────────────────────

class TestConcatAssembly:
    """The assembled filter graph is a concat of consecutive pieces: one
    clip per segment, one color clip per uncovered stretch.  Every output
    frame flows through exactly one piece — the graph must not contain the
    old per-segment overlay chain."""

    @pytest.fixture
    def captured(self, monkeypatch):
        calls = {}

        class _Result:
            returncode = 0
            stderr     = b''

        def fake_run(cmd, **_kw):
            calls['cmd'] = cmd
            return _Result()

        monkeypatch.setattr(video_transcode.subprocess, 'run', fake_run)
        monkeypatch.setattr(video_transcode, '_encoder_args',
                            lambda: ['-c:v', 'libx264'])
        return calls

    def _transcode(self, stage_dir, start_ts, end_ts):
        transcode_to_video(
            str(stage_dir), start_ts, end_ts, 0xFF112233,
            os.path.join(str(stage_dir), 'out.mp4'),
            _query_info=lambda _p: (600.0, 1280, 720, '30/1'),
        )

    def _filter(self, calls):
        cmd = calls['cmd']
        return cmd[cmd.index('-filter_complex') + 1]

    def test_no_overlay_chain(self, tmp_path, captured):
        now = time.time()
        _make_renamed(tmp_path, now - 800, now - 500)
        _make_renamed(tmp_path, now - 400, now - 100)
        self._transcode(tmp_path, now - 900, now)
        assert 'overlay' not in self._filter(captured)
        assert 'concat=' in self._filter(captured)

    def test_gaps_become_color_pieces(self, tmp_path, captured):
        # window [now-900, now]; segments [now-800, now-500], [now-400, now-100]
        # → gap, seg, gap, seg, gap = 5 pieces, 3 of them color.
        now = time.time()
        _make_renamed(tmp_path, now - 800, now - 500)
        _make_renamed(tmp_path, now - 400, now - 100)
        self._transcode(tmp_path, now - 900, now)
        filt = self._filter(captured)
        assert 'concat=n=5:v=1:a=0' in filt
        assert filt.count('color=c=') == 3

    def test_full_coverage_has_no_color_piece(self, tmp_path, captured):
        now = time.time()
        _make_renamed(tmp_path, now - 1000, now)
        self._transcode(tmp_path, now - 900, now - 100)
        filt = self._filter(captured)
        assert 'concat=n=1:v=1:a=0' in filt
        assert 'color=c=' not in filt  # no gap pieces (tpad's color= is fine)

    def test_head_trim_uses_input_seek(self, tmp_path, captured):
        """A segment starting before the window is opened with -ss so its
        pre-window content is skipped at the demuxer, not decoded."""
        now = time.time()
        _make_renamed(tmp_path, now - 1000, now)
        self._transcode(tmp_path, now - 900, now - 100)
        cmd = captured['cmd']
        idx = cmd.index('-ss')
        assert float(cmd[idx + 1]) == pytest.approx(100.0, abs=0.01)
        assert cmd[idx + 2] == '-i'

    def test_no_input_seek_when_segment_starts_inside_window(
            self, tmp_path, captured):
        now = time.time()
        _make_renamed(tmp_path, now - 400, now - 100)
        self._transcode(tmp_path, now - 900, now)
        assert '-ss' not in captured['cmd']

    def test_no_segments_pure_color(self, tmp_path, captured):
        now = time.time()
        self._transcode(tmp_path, now - 300, now)
        filt = self._filter(captured)
        assert filt.count('color=c=') == 1
        assert 'concat=n=1:v=1:a=0' in filt
        assert '-i' not in captured['cmd']

    def test_inputs_appear_in_timeline_order(self, tmp_path, captured):
        now = time.time()
        first,  _ = _make_renamed(tmp_path, now - 800, now - 500)
        second, _ = _make_renamed(tmp_path, now - 400, now - 100)
        self._transcode(tmp_path, now - 900, now)
        cmd = captured['cmd']
        inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == '-i']
        assert inputs == [first, second]

    def test_segment_pieces_are_padded_against_undershoot(
            self, tmp_path, captured):
        """Each clip is tpad-ed with fill color then cut to its planned
        length, so short real content can't shift later pieces."""
        now = time.time()
        _make_renamed(tmp_path, now - 800, now - 500)
        self._transcode(tmp_path, now - 900, now)
        assert 'tpad=stop_mode=add' in self._filter(captured)
