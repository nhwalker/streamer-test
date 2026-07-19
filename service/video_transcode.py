"""
video_transcode.py -- ffmpeg-based video assembly for the /video endpoint.

Takes a directory of staged .mp4 segments and produces a single output .mp4
that exactly covers the requested time window:
  - the output is a concat of pieces in timeline order: each overlapping
    segment contributes one clip, and every uncovered stretch (leading,
    between segments, trailing) becomes a generated solid-color clip, so
    each frame is processed once regardless of how many segments there are
  - segments starting before the window are opened with an input-side seek
    (-ss), so the pre-window content is skipped at the demuxer instead of
    decoded and discarded; segments extending past the end are trimmed
  - a segment whose real content is shorter than its filename window claims
    (crash-truncated tail, estimated active-segment end) is padded with the
    fill color so every later piece still lands at its true temporal offset
  - the output is always a complete, faststart MP4 (moov atom at the front)
    so web players can begin decoding from the first received bytes

All files in the stage directory must have timestamps in their filenames
(as produced by stage_segments in archive_export.py).  Files without recognized
timestamps are ignored.

Requires: ffmpeg and ffprobe available in PATH.

Public API:
    transcode_to_video(stage_dir, start_ts, end_ts,
                       fill_color_argb, output_path,
                       default_width, default_height)
"""
import functools
import json
import os
import subprocess
from dataclasses import dataclass

from archive_times import parse_segment_times
from encoders import nvenc_works as _nvenc_works

_DEFAULT_FPS = '25/1'

# /video output uses CRF/CQP (quality-targeted) so the assembled MP4 preserves
# the archive's quality on download.  The default tracks ARCHIVE_QP so the two
# stay in sync without a second knob to tune; can be overridden with VIDEO_QP.
# Lower is better; 18 is the conventional visually-lossless threshold for H.264.
_VIDEO_QP = int(os.environ.get('VIDEO_QP', os.environ.get('ARCHIVE_QP', '18')))


def _detect_encoder_args():
    """Return ffmpeg output-encoder args for the best available video encoder.

    Uses quality-targeted (CRF / NVENC constqp) modes so the assembled
    /video output preserves whatever quality the archive segments carry
    rather than capping at the legacy 6 Mbps VBR.
    """
    qp = str(_VIDEO_QP)
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return ['-c:v', 'libx264', '-preset', 'medium',
                '-crf', qp, '-pix_fmt', 'yuv420p']
    encoders = result.stdout
    if 'h264_nvenc' in encoders and _nvenc_works():
        return ['-c:v', 'h264_nvenc', '-preset', 'p5',
                '-rc', 'constqp', '-qp', qp]
    if 'libx264' in encoders:
        return ['-c:v', 'libx264', '-preset', 'medium',
                '-crf', qp, '-pix_fmt', 'yuv420p']
    if 'mpeg4' in encoders:
        return ['-c:v', 'mpeg4', '-q:v', '5']
    return ['-c:v', 'ffv1']


# Lazy so that merely importing this module never spawns ffmpeg probes —
# only the first actual transcode pays for encoder detection.
_encoder_args = functools.lru_cache(maxsize=None)(_detect_encoder_args)


@dataclass
class TimelineItem:
    path:           str
    offset_s:       float  # > 0 → seek this far into the file (pre-window content)
    output_start_s: float  # seconds from start of output where the clip begins
    duration_s:     float  # clip length after clipping to the window


# ── File introspection ────────────────────────────────────────────────────────

def _query_file_info(path):
    """Return (duration_s, width, height, fps_str) via ffprobe."""
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-analyzeduration', '10000000', '-probesize', '10000000',
         '-show_streams', '-show_format', path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return 0.0, 0, 0, _DEFAULT_FPS
    duration_s = float(data.get('format', {}).get('duration') or 0)
    width = height = 0
    fps_str = _DEFAULT_FPS
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video':
            width   = int(stream.get('width',  0))
            height  = int(stream.get('height', 0))
            fps_str = stream.get('r_frame_rate', _DEFAULT_FPS)
            break
    return duration_s, width, height, fps_str


# ── Timeline builder ──────────────────────────────────────────────────────────

def _build_timeline(stage_dir, start_ts, end_ts):
    """Build an ordered list of TimelineItem covering [start_ts, end_ts].

    All files are expected to have timestamps in their names (as produced by
    stage_segments).  Files without a recognized timestamp pattern are skipped.

    Returns [] when no segments overlap (pure color output).
    """
    timeline = []
    for fname in os.listdir(stage_dir):
        if not fname.endswith('.mp4'):
            continue
        times = parse_segment_times(fname)
        if times is None:
            continue
        seg_start, seg_end = times
        if seg_start >= end_ts or seg_end <= start_ts:
            continue
        fpath = os.path.join(stage_dir, fname)
        clip_start = max(seg_start, start_ts)
        clip_end   = min(seg_end, end_ts)
        timeline.append(TimelineItem(
            path           = fpath,
            offset_s       = clip_start - seg_start,
            output_start_s = clip_start - start_ts,
            duration_s     = clip_end - clip_start,
        ))
    timeline.sort(key=lambda x: x.output_start_s)
    return timeline


# ── ffmpeg assembly ───────────────────────────────────────────────────────────

# Gaps and clip slivers below this many seconds are timestamp noise, not
# real coverage differences; they are absorbed rather than rendered.
_EPS = 1e-3


def _plan_pieces(timeline, total_dur):
    """Split [0, total_dur] into an ordered piece list covering it exactly.

    Returns ('seg', item, seek_s, dur_s) and ('gap', dur_s) tuples.  A
    segment overlapped by the previous piece (clock-estimation slop on the
    active segment) has its head advanced; gaps arise wherever no segment
    covers the output.
    """
    pieces = []
    pos = 0.0
    for item in timeline:
        start, dur, seek = item.output_start_s, item.duration_s, item.offset_s
        if start > pos + _EPS:
            pieces.append(('gap', start - pos))
            pos = start
        overlap = pos - start   # > 0 when the previous piece covers our head
        if overlap > 0:
            seek += overlap
            dur  -= overlap
        if dur <= _EPS:
            continue
        pieces.append(('seg', item, seek, dur))
        pos += dur
    if total_dur - pos > _EPS or not pieces:
        pieces.append(('gap', total_dur - pos))
    return pieces


def transcode_to_video(stage_dir, start_ts, end_ts,
                       fill_color_argb, output_path,
                       default_width=1920, default_height=1080,
                       _query_info=None):
    """Assemble a single MP4 covering [start_ts, end_ts] from staged segments.

    The window is cut into consecutive pieces — one clip per overlapping
    segment, one generated fill_color_argb (0xAARRGGBB) clip per uncovered
    stretch — which are normalised to a common size/rate/format and joined
    with the concat filter, so every output frame is produced by exactly one
    piece (the old overlay-chain approach ran each frame through one filter
    per segment).  The output is written with `-movflags +faststart` so web
    players can start playback before the full file has been received.
    output_path will be overwritten.  Raises RuntimeError on ffmpeg failure.
    """
    if _query_info is None:
        _query_info = _query_file_info

    timeline = _build_timeline(stage_dir, start_ts, end_ts)

    width, height, fps_str = default_width, default_height, _DEFAULT_FPS
    for item in timeline:
        _, w, h, fps = _query_info(item.path)
        if w > 0 and h > 0:
            width, height, fps_str = w, h, fps
            break

    r = (fill_color_argb >> 16) & 0xFF
    g = (fill_color_argb >>  8) & 0xFF
    b =  fill_color_argb        & 0xFF
    color     = f'0x{r:02X}{g:02X}{b:02X}'
    size      = f'{width}x{height}'
    total_dur = end_ts - start_ts

    pieces = _plan_pieces(timeline, total_dur)

    cmd     = ['ffmpeg', '-y']
    filters = []
    labels  = []
    n_inputs = 0
    for k, piece in enumerate(pieces):
        label = f'[p{k}]'
        if piece[0] == 'gap':
            _, dur = piece
            filters.append(
                f'color=c={color}:s={size}:r={fps_str}'
                f':duration={dur:.6f},format=yuv420p,setsar=1{label}'
            )
        else:
            _, item, seek, dur = piece
            in_idx = n_inputs
            n_inputs += 1
            if seek > _EPS:
                # Input-side seek: decode from the nearest prior keyframe
                # instead of decoding and discarding the whole head.
                cmd += ['-ss', f'{seek:.6f}']
            cmd += ['-i', item.path]
            filters.append(
                f'[{in_idx}:v]setpts=PTS-STARTPTS'
                f',scale={width}:{height},setsar=1,fps={fps_str}'
                f',format=yuv420p'
                # Undershoot guard: pad with fill color, then cut to the
                # planned length, so a segment carrying less real content
                # than its filename window claims cannot shift every
                # later piece off its true temporal offset.
                f',tpad=stop_mode=add:stop_duration={dur:.6f}:color={color}'
                f',trim=end={dur:.6f},setpts=PTS-STARTPTS{label}'
            )
        labels.append(label)
    filters.append(''.join(labels) + f'concat=n={len(pieces)}:v=1:a=0[out]')

    cmd += ['-filter_complex', ';'.join(filters)]
    cmd += ['-map', '[out]', *_encoder_args()]
    cmd += ['-movflags', '+faststart', output_path]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace')[-2000:]
        raise RuntimeError(
            f'ffmpeg failed (exit {result.returncode}): {stderr}'
        )
