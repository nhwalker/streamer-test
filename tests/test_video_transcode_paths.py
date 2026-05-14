"""
Unit tests for the /video fast-path additions in video_transcode.py:

  • _detect_decoder_args  — NVDEC input flags are only enabled when both
                             NVENC and NVDEC work; otherwise CPU decode.
  • _can_stream_copy      — gates the concat+copy fast path; must reject
                             windows that need head trim or have gaps.
  • _stream_copy_listing  — concat demuxer text format, single-quote
                             escaping intact.
  • _stream_copy_cmd      — ffmpeg arg ordering and `-c copy`/+faststart.
  • _build_reencode_filtergraph
                          — CPU graph uses overlay; CUDA graph uses
                             overlay_cuda + hwupload_cuda on the color base.

No ffmpeg or GPU required.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
import video_transcode as vt  # noqa: E402
from video_transcode import TimelineItem  # noqa: E402


# ── _detect_decoder_args ─────────────────────────────────────────────────────

class TestDetectDecoderArgs:

    def test_cpu_encoder_returns_empty(self, monkeypatch):
        """libx264 encoder → no hwaccel input flags, CPU filters."""
        monkeypatch.setattr(vt, '_nvdec_works', lambda: True)
        args, use_cuda = vt._detect_decoder_args(
            ['-c:v', 'libx264', '-preset', 'medium', '-crf', '18'],
        )
        assert args == []
        assert use_cuda is False

    def test_nvenc_but_no_nvdec_returns_empty(self, monkeypatch):
        """NVENC works but NVDEC missing → don't use cuda graph
        (the hwupload/hwdownload round trip would dominate)."""
        monkeypatch.setattr(vt, '_nvdec_works', lambda: False)
        args, use_cuda = vt._detect_decoder_args(
            ['-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'constqp', '-qp', '18'],
        )
        assert args == []
        assert use_cuda is False

    def test_nvenc_and_nvdec_both_work(self, monkeypatch):
        """Both functional → emit cuda hwaccel + use_cuda True."""
        monkeypatch.setattr(vt, '_nvdec_works', lambda: True)
        args, use_cuda = vt._detect_decoder_args(
            ['-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'constqp', '-qp', '18'],
        )
        assert args == ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda']
        assert use_cuda is True


# ── _can_stream_copy ─────────────────────────────────────────────────────────

def _item(path, seg_start, seg_end, start_ts):
    """Convenience: build a TimelineItem mirroring _build_timeline's math."""
    clip_start = max(seg_start, start_ts)
    return TimelineItem(
        path           = path,
        offset_s       = clip_start - seg_start,
        output_start_s = clip_start - start_ts,
        seg_start      = seg_start,
        seg_end        = seg_end,
    )


class TestCanStreamCopy:

    def test_empty_timeline_rejected(self):
        assert vt._can_stream_copy([], 100.0, 200.0) is False

    def test_aligned_single_segment_accepted(self):
        """seg covers exactly [100, 200] — no trim, no gap → copy."""
        tl = [_item('a.mp4', 100.0, 200.0, 100.0)]
        assert vt._can_stream_copy(tl, 100.0, 200.0) is True

    def test_head_trim_required_rejected(self):
        """Segment starts well before start_ts → head trim → no copy."""
        tl = [_item('a.mp4', 50.0, 200.0, 100.0)]
        assert vt._can_stream_copy(tl, 100.0, 200.0) is False

    def test_leading_gap_rejected(self):
        """Segment starts after start_ts → leading gap → no copy."""
        tl = [_item('a.mp4', 150.0, 250.0, 100.0)]
        assert vt._can_stream_copy(tl, 100.0, 250.0) is False

    def test_contiguous_pair_accepted(self):
        tl = [
            _item('a.mp4', 100.0, 200.0, 100.0),
            _item('b.mp4', 200.0, 300.0, 100.0),
        ]
        assert vt._can_stream_copy(tl, 100.0, 300.0) is True

    def test_middle_gap_rejected(self):
        """Two segments with a >tol gap between them → no copy."""
        tl = [
            _item('a.mp4', 100.0, 200.0, 100.0),
            _item('b.mp4', 220.0, 320.0, 100.0),
        ]
        assert vt._can_stream_copy(tl, 100.0, 320.0) is False

    def test_trailing_gap_rejected(self):
        """Last segment ends before end_ts → trailing gap → no copy."""
        tl = [_item('a.mp4', 100.0, 200.0, 100.0)]
        assert vt._can_stream_copy(tl, 100.0, 250.0) is False

    def test_within_tolerance_accepted(self):
        """Sub-tol seam mismatches are tolerated (output overshoots by ≤tol)."""
        tl = [
            _item('a.mp4', 99.9, 200.0, 99.9),   # head within tol
            _item('b.mp4', 200.2, 300.0, 99.9),  # seam within tol
        ]
        assert vt._can_stream_copy(tl, 100.0, 299.8) is True


# ── _stream_copy_listing / _stream_copy_cmd ─────────────────────────────────

class TestStreamCopyArtifacts:

    def test_listing_one_line_per_segment(self):
        tl = [
            _item('/stage/a.mp4', 0.0, 10.0, 0.0),
            _item('/stage/b.mp4', 10.0, 20.0, 0.0),
        ]
        listing = vt._stream_copy_listing(tl)
        assert listing == "file '/stage/a.mp4'\nfile '/stage/b.mp4'\n"

    def test_listing_escapes_single_quote(self):
        """ffmpeg concat format requires '\\'' to embed a single quote."""
        tl = [_item("/stage/o'reilly.mp4", 0.0, 10.0, 0.0)]
        listing = vt._stream_copy_listing(tl)
        assert listing == "file '/stage/o'\\''reilly.mp4'\n"

    def test_cmd_is_copy_with_faststart(self):
        cmd = vt._stream_copy_cmd('/tmp/list.txt', 123.5, '/out/video.mp4')
        assert cmd[0] == 'ffmpeg'
        assert '-c' in cmd and cmd[cmd.index('-c') + 1] == 'copy'
        assert '-movflags' in cmd
        assert cmd[cmd.index('-movflags') + 1] == '+faststart'
        assert cmd[-1] == '/out/video.mp4'
        # -t carries the exact total_dur formatted to 6dp
        assert '-t' in cmd
        assert cmd[cmd.index('-t') + 1] == '123.500000'
        # concat demuxer flags
        assert '-f' in cmd and cmd[cmd.index('-f') + 1] == 'concat'
        assert '-safe' in cmd and cmd[cmd.index('-safe') + 1] == '0'


# ── _build_reencode_filtergraph ─────────────────────────────────────────────

class TestFiltergraphCpu:

    def test_empty_timeline_pure_color(self):
        graph = vt._build_reencode_filtergraph(
            timeline=[], color='0x000000', size='1920x1080',
            fps_str='25/1', total_dur=10.0, use_cuda=False,
        )
        assert 'color=c=0x000000' in graph
        assert '[out]' in graph
        assert 'overlay' not in graph     # no overlay when there are no inputs
        assert 'hwupload_cuda' not in graph

    def test_overlay_used_when_not_cuda(self):
        tl = [_item('/s/a.mp4', 100.0, 200.0, 100.0)]
        graph = vt._build_reencode_filtergraph(
            timeline=tl, color='0xFF0000', size='1280x720',
            fps_str='30/1', total_dur=100.0, use_cuda=False,
        )
        assert 'overlay=eof_action=pass[out]' in graph
        assert 'overlay_cuda' not in graph
        assert 'hwupload_cuda' not in graph


class TestFiltergraphCuda:

    def test_cuda_uses_overlay_cuda_and_hwupload(self):
        tl = [_item('/s/a.mp4', 100.0, 200.0, 100.0)]
        graph = vt._build_reencode_filtergraph(
            timeline=tl, color='0x000000', size='1280x720',
            fps_str='25/1', total_dur=100.0, use_cuda=True,
        )
        assert 'overlay_cuda=eof_action=pass[out]' in graph
        assert 'hwupload_cuda' in graph
        # The base must be NV12-formatted before hwupload_cuda; otherwise the
        # GPU will not accept the frame.
        assert 'format=nv12,hwupload_cuda' in graph
        # Plain `overlay=` (CPU) must NOT appear in the cuda graph.
        assert 'overlay=eof_action' not in graph

    def test_cuda_multi_segment_chain(self):
        """Two-segment graph chains overlay_cuda → overlay_cuda → [out]."""
        tl = [
            _item('/s/a.mp4', 100.0, 200.0, 100.0),
            _item('/s/b.mp4', 200.0, 300.0, 100.0),
        ]
        graph = vt._build_reencode_filtergraph(
            timeline=tl, color='0x000000', size='1280x720',
            fps_str='25/1', total_dur=200.0, use_cuda=True,
        )
        # First overlay_cuda outputs to [t0], second outputs to [out].
        assert 'overlay_cuda=eof_action=pass[t0]' in graph
        assert 'overlay_cuda=eof_action=pass[out]' in graph
        # And both clips are referenced.
        assert '[c0]' in graph and '[c1]' in graph
