"""
video_transcode.py -- ffmpeg-based video assembly for the /video endpoint.

Takes a directory of staged .mp4 segments and produces a single output .mp4
that exactly covers the requested time window:
  - a full-duration solid-color base video covers the entire window
  - each segment is overlaid at its correct temporal position
  - segments starting before the window are trimmed; segments extending past
    the end are cut off implicitly when the base video ends
  - the output is always a complete, faststart MP4 (moov atom at the front)
    so web players can begin decoding from the first received bytes

All files in the stage directory must have timestamps in their filenames
(as produced by stage_segments in web_server.py).  Files without recognized
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
import tempfile
from dataclasses import dataclass

from archive_times import parse_segment_times

_DEFAULT_FPS = '25/1'

# /video output uses CRF/CQP (quality-targeted) so the assembled MP4 preserves
# the archive's quality on download.  The default tracks ARCHIVE_QP so the two
# stay in sync without a second knob to tune; can be overridden with VIDEO_QP.
# Lower is better; 18 is the conventional visually-lossless threshold for H.264.
_VIDEO_QP = int(os.environ.get('VIDEO_QP', os.environ.get('ARCHIVE_QP', '18')))


@functools.lru_cache(maxsize=None)
def _nvenc_works():
    """Return True if h264_nvenc can actually encode (GPU present and functional)."""
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner',
             '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.04',
             '-frames:v', '1', '-c:v', 'h264_nvenc', '-f', 'null', '-'],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@functools.lru_cache(maxsize=None)
def _nvdec_works():
    """Return True if NVDEC is available — checked once at first call.

    Gates the CUDA filter graph: without functional NVDEC, decoding stays on
    CPU and overlay_cuda would require an extra hwupload per frame, wiping
    out the win.  We require NVENC to work first (proves the NVIDIA driver
    is functional) and h264_cuvid to be listed in `ffmpeg -decoders` (proves
    the build has CUVID support compiled in).
    """
    if not _nvenc_works():
        return False
    try:
        r = subprocess.run(
            ['ffmpeg', '-hide_banner', '-decoders'],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return 'h264_cuvid' in r.stdout


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


_ENCODER_ARGS = _detect_encoder_args()


def _detect_decoder_args(encoder_args=None):
    """Return (input_args, use_cuda_filters) for matching the encoder.

    When the encoder is NVENC *and* NVDEC works, pre-pend cuda hwaccel flags
    to every input so frames stay on the GPU through the decode → filter →
    encode pipeline.  Otherwise input flags are empty and the filter graph
    runs on CPU.  We don't enable NVDEC without NVENC because the result
    would have to be downloaded for libx264, which costs more than it saves.
    """
    if encoder_args is None:
        encoder_args = _ENCODER_ARGS
    if 'h264_nvenc' not in encoder_args:
        return [], False
    if not _nvdec_works():
        return [], False
    return ['-hwaccel', 'cuda', '-hwaccel_output_format', 'cuda'], True


_DECODER_ARGS, _USE_CUDA_FILTERS = _detect_decoder_args()


# ── Stream-copy fast path ─────────────────────────────────────────────────────

# Tolerance (seconds) for the gap-free coverage check.  Tighter than a single
# GOP at 25 fps so a near-aligned window doesn't accidentally include >1 s of
# pre-window content at the head.
_COPY_TOL_S = 0.5


def _can_stream_copy(timeline, start_ts, end_ts, tol=_COPY_TOL_S):
    """True iff [start_ts, end_ts] is contiguously covered with no head trim.

    The stream-copy path uses ffmpeg's concat demuxer with `-c copy` — no
    re-encoding.  It is roughly 1-2 orders of magnitude faster than the
    overlay/encode path, but requires:
      • The first segment starts at or after start_ts within `tol` (we cannot
        trim mid-GOP without re-encoding).
      • No gaps between consecutive segments (within `tol`).
      • The last segment extends to at least end_ts (within `tol`).
    When True the output may run up to `tol` seconds longer than requested at
    the leading edge; output `-t` trims the trailing edge.
    """
    if not timeline:
        return False
    if timeline[0].offset_s > tol:
        return False
    if timeline[0].output_start_s > tol:
        return False
    for prev, curr in zip(timeline, timeline[1:]):
        if curr.seg_start - prev.seg_end > tol:
            return False
    if end_ts - timeline[-1].seg_end > tol:
        return False
    return True


def _stream_copy_listing(timeline):
    """Return the ffmpeg concat demuxer listing as a string.

    Single-quotes inside paths are escaped per ffmpeg's concat format.
    """
    lines = []
    for item in timeline:
        escaped = item.path.replace("'", r"'\''")
        lines.append(f"file '{escaped}'\n")
    return ''.join(lines)


def _stream_copy_cmd(listing_path, total_dur, output_path):
    """Build the ffmpeg command for the concat-demuxer stream-copy path."""
    return ['ffmpeg', '-y',
            '-f', 'concat', '-safe', '0', '-i', listing_path,
            '-t', f'{total_dur:.6f}',
            '-c', 'copy',
            '-movflags', '+faststart',
            output_path]


@dataclass
class TimelineItem:
    path:           str
    offset_s:       float  # > 0 → trim=start=offset_s to skip pre-window content
    output_start_s: float  # seconds from start of output where overlay begins
    seg_start:      float = 0.0  # epoch s — the segment's own recorded start
    seg_end:        float = 0.0  # epoch s — the segment's own recorded end


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
        timeline.append(TimelineItem(
            path           = fpath,
            offset_s       = clip_start - seg_start,
            output_start_s = clip_start - start_ts,
            seg_start      = seg_start,
            seg_end        = seg_end,
        ))
    timeline.sort(key=lambda x: x.output_start_s)
    return timeline


# ── Filter graph for the re-encode path ───────────────────────────────────────

def _build_reencode_filtergraph(timeline, color, size, fps_str, total_dur,
                                use_cuda):
    """Return the `-filter_complex` string for the re-encode path.

    use_cuda switches the base source onto the GPU (color → NV12 → hwupload)
    and uses overlay_cuda so all compositing happens on the GPU.  Inputs are
    expected to already be on the GPU (caller pre-pends `-hwaccel cuda
    -hwaccel_output_format cuda` per -i).  When use_cuda is False the graph
    is the original CPU version.
    """
    if not timeline:
        return (f'color=c={color}:s={size}:r={fps_str}'
                f':duration={total_dur:.6f},setpts=PTS-STARTPTS[out]')

    filters = []
    if use_cuda:
        filters.append(
            f'color=c={color}:s={size}:r={fps_str}'
            f':duration={total_dur:.6f},format=nv12,'
            f'hwupload_cuda,setpts=PTS-STARTPTS[base]'
        )
    else:
        filters.append(
            f'color=c={color}:s={size}:r={fps_str}'
            f':duration={total_dur:.6f},setpts=PTS-STARTPTS[base]'
        )

    overlay = 'overlay_cuda' if use_cuda else 'overlay'

    for i, item in enumerate(timeline):
        label = f'[c{i}]'
        y     = item.output_start_s
        if item.offset_s > 0:
            filters.append(
                f'[{i}:v]trim=start={item.offset_s:.6f}'
                f',setpts=PTS-STARTPTS+{y:.6f}/TB{label}'
            )
        else:
            filters.append(
                f'[{i}:v]setpts=PTS-STARTPTS+{y:.6f}/TB{label}'
            )

    prev = 'base'
    for i in range(len(timeline)):
        src  = f'[{prev}]' if prev == 'base' else prev
        clip = f'[c{i}]'
        out  = '[out]' if i == len(timeline) - 1 else f'[t{i}]'
        filters.append(f'{src}{clip}{overlay}=eof_action=pass{out}')
        prev = f'[t{i}]'

    return ';'.join(filters)


# ── ffmpeg assembly ───────────────────────────────────────────────────────────

def _try_stream_copy(timeline, total_dur, output_path):
    """Attempt the concat+copy fast path; return True on success, False otherwise.

    A False return means the caller should fall back to the re-encode path
    (e.g. codec parameters didn't actually match across segments).  Caller is
    responsible for re-attempting with the full pipeline.
    """
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt',
                                     delete=False, prefix='concat_') as fh:
        fh.write(_stream_copy_listing(timeline))
        listing = fh.name
    try:
        cmd = _stream_copy_cmd(listing, total_dur, output_path)
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace')[-500:]
            print(f'[video] stream-copy fast path failed, falling back: '
                  f'{stderr}', flush=True)
            return False
        return True
    finally:
        try:
            os.unlink(listing)
        except FileNotFoundError:
            pass


def transcode_to_video(stage_dir, start_ts, end_ts,
                       fill_color_argb, output_path,
                       default_width=1920, default_height=1080,
                       _query_info=None):
    """Assemble a single MP4 covering [start_ts, end_ts] from staged segments.

    Three execution paths, picked automatically:
      1. Stream-copy fast path (`-c copy` via concat demuxer) when the staged
         segments contiguously cover the window with no head trim needed.
         No re-encoding — typically seconds even for hour-long windows.
      2. CUDA re-encode (NVDEC → overlay_cuda → NVENC) when NVENC and NVDEC
         are both functional.  All compositing stays on the GPU.
      3. CPU re-encode (libx264 / mpeg4 / ffv1 fallback) otherwise.

    A solid fill_color_argb (0xAARRGGBB) base video covers the full duration.
    Each segment is overlaid at its correct temporal position.  Gaps between
    segments and at the edges are filled by the base.  The output is written
    with `-movflags +faststart` so web players can start playback before the
    full file has been received.
    output_path will be overwritten.  Raises RuntimeError on ffmpeg failure.
    """
    if _query_info is None:
        _query_info = _query_file_info

    timeline = _build_timeline(stage_dir, start_ts, end_ts)
    total_dur = end_ts - start_ts

    if _can_stream_copy(timeline, start_ts, end_ts):
        if _try_stream_copy(timeline, total_dur, output_path):
            return
        # Fall through to the re-encode path on copy failure.

    width, height, fps_str = default_width, default_height, _DEFAULT_FPS
    for item in timeline:
        _, w, h, fps = _query_info(item.path)
        if w > 0 and h > 0:
            width, height, fps_str = w, h, fps
            break

    r = (fill_color_argb >> 16) & 0xFF
    g = (fill_color_argb >>  8) & 0xFF
    b =  fill_color_argb        & 0xFF
    color = f'0x{r:02X}{g:02X}{b:02X}'
    size  = f'{width}x{height}'

    cmd = ['ffmpeg', '-y']
    for item in timeline:
        cmd += list(_DECODER_ARGS) + ['-i', item.path]

    filtergraph = _build_reencode_filtergraph(
        timeline, color, size, fps_str, total_dur, _USE_CUDA_FILTERS,
    )

    cmd += ['-filter_complex', filtergraph]
    cmd += ['-map', '[out]'] + _ENCODER_ARGS
    cmd += ['-movflags', '+faststart', output_path]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace')[-2000:]
        raise RuntimeError(
            f'ffmpeg failed (exit {result.returncode}): {stderr}'
        )
