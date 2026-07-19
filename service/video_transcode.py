"""
video_transcode.py -- ffmpeg-based video assembly for the /video endpoint.

Takes a directory of staged .mp4 segments and produces a single output .mp4
that exactly covers the requested time window:
  - the output is a concat of pieces in timeline order: each overlapping
    segment contributes one clip, and every uncovered stretch (leading,
    between segments, trailing) becomes a generated solid-color clip, so
    each frame is processed once regardless of how many segments there are
  - whole segments that need no trimming are stream-copied via MPEG-TS
    intermediates and the concat demuxer — their bitstream is never
    decoded or re-encoded; only gap fills and window-clipped boundary
    segments pay for an encode
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


@dataclass
class Piece:
    """One consecutive slice of the output timeline (see _plan_pieces).

    kind    'seg' — a clip from a staged segment; 'gap' — generated fill.
    dur_s   length of this piece on the output timeline.
    item    the TimelineItem behind a 'seg' piece (None for gaps).
    seek_s  input-side seek into item.path before the clip starts.  Not
            the same as item.offset_s: it additionally includes any head
            clamp applied when the previous piece already covers this
            segment's start.
    """
    kind:   str
    dur_s:  float
    item:   TimelineItem | None = None
    seek_s: float = 0.0


@dataclass
class _Canvas:
    """The output raster every piece is normalised to, plus the gap fill.

    `color` is an ffmpeg 0xRRGGBB string (see _fill_color_str)."""
    width:   int
    height:  int
    fps_str: str
    color:   str

    @property
    def size(self):
        return f'{self.width}x{self.height}'


def _fill_color_str(argb):
    """ffmpeg 0xRRGGBB color string from a 0xAARRGGBB int.

    The alpha byte is deliberately dropped: the output raster has no
    alpha channel, so only the RGB part of VIDEO_FILL_COLOR is used.
    """
    return f'0x{argb & 0xFFFFFF:06X}'


# ── File introspection ────────────────────────────────────────────────────────

def _query_file_info(path):
    """Return (duration_s, width, height, fps_str, codec_name) via ffprobe."""
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-analyzeduration', '10000000', '-probesize', '10000000',
         '-show_streams', '-show_format', path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return 0.0, 0, 0, _DEFAULT_FPS, ''
    duration_s = float(data.get('format', {}).get('duration') or 0)
    width = height = 0
    fps_str = _DEFAULT_FPS
    codec   = ''
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video':
            width   = int(stream.get('width',  0))
            height  = int(stream.get('height', 0))
            fps_str = stream.get('r_frame_rate', _DEFAULT_FPS)
            codec   = stream.get('codec_name') or ''
            break
    return duration_s, width, height, fps_str, codec


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

# A segment qualifies for the stream-copy fast path only if its probed
# content duration matches the duration its filename claims within this
# tolerance.  The encode path pads undershooting segments with fill color
# (tpad); the copy path cannot, so a mismatched segment (estimated
# active-segment end, crash-truncated tail) would shift every later piece
# off its true wall-clock offset.
_COPY_DUR_TOL_SEC = 0.5


def _run_ffmpeg(cmd):
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode('utf-8', errors='replace')[-2000:]
        raise RuntimeError(
            f'ffmpeg failed (exit {result.returncode}): {stderr}'
        )


def _plan_pieces(timeline, total_dur):
    """Split [0, total_dur] into an ordered Piece list covering it exactly.

    Gaps arise wherever no segment covers the output; a segment whose
    head is already covered by the previous piece (clock-estimation slop
    on the active segment) has its clip head advanced instead, so output
    time never runs backwards.
    """
    pieces = []
    pos = 0.0
    for item in timeline:
        if item.output_start_s > pos + _EPS:
            pieces.append(Piece('gap', item.output_start_s - pos))
            pos = item.output_start_s
        head_clamp = max(0.0, pos - item.output_start_s)
        dur = item.duration_s - head_clamp
        if dur <= _EPS:
            continue   # entirely covered by the previous piece
        pieces.append(Piece('seg', dur, item=item,
                            seek_s=item.offset_s + head_clamp))
        pos += dur
    if total_dur - pos > _EPS or not pieces:
        # Trailing fill.  For a degenerate window (total_dur ≈ 0 with an
        # empty timeline) this deliberately appends a zero-length gap:
        # ffmpeg rejects a zero-duration color source, so the request
        # fails the way it always has (RuntimeError → HTTP 500).
        pieces.append(Piece('gap', total_dur - pos))
    return pieces


def _gap_filter(dur, canvas, label):
    """Generated solid-color source for an uncovered stretch."""
    return (f'color=c={canvas.color}:s={canvas.size}:r={canvas.fps_str}'
            f':duration={dur:.6f},format=yuv420p,setsar=1{label}')


def _seg_filter(in_idx, dur, canvas, label):
    """Normalisation chain for a segment clip (input already seeked).

    The tpad+trim pair is the undershoot guard: pad with fill color, then
    cut to the planned length, so a segment carrying less real content
    than its filename window claims cannot shift every later piece off
    its true temporal offset.
    """
    return (f'[{in_idx}:v]setpts=PTS-STARTPTS'
            f',scale={canvas.width}:{canvas.height},setsar=1'
            f',fps={canvas.fps_str},format=yuv420p'
            f',tpad=stop_mode=add:stop_duration={dur:.6f}:color={canvas.color}'
            f',trim=end={dur:.6f},setpts=PTS-STARTPTS{label}')


def _is_stream_copyable(piece, canvas, _query_info):
    """True when a piece can be stream-copied instead of re-encoded.

    Requires an untrimmed whole-segment piece whose probed content is
    H.264 at the output resolution and actually carries the duration its
    filename claims (see _COPY_DUR_TOL_SEC).
    """
    if piece.kind != 'seg':
        return False
    if piece.seek_s > _EPS or piece.item.offset_s > _EPS:
        return False
    times = parse_segment_times(os.path.basename(piece.item.path))
    if times is None:
        return False
    seg_dur = times[1] - times[0]
    if abs(piece.dur_s - seg_dur) > _EPS:
        return False   # clipped by the window or a neighbouring piece
    probed_dur, w, h, _fps, codec = _query_info(piece.item.path)
    if codec != 'h264' or (w, h) != (canvas.width, canvas.height):
        return False
    return abs(probed_dur - seg_dur) <= _COPY_DUR_TOL_SEC


def _encode_single_pass(pieces, canvas, output_path):
    """One ffmpeg run: every piece decoded/generated, concat filter, encode."""
    cmd     = ['ffmpeg', '-y']
    filters = []
    labels  = []
    n_inputs = 0
    for k, piece in enumerate(pieces):
        label = f'[p{k}]'
        if piece.kind == 'gap':
            filters.append(_gap_filter(piece.dur_s, canvas, label))
        else:
            in_idx = n_inputs
            n_inputs += 1
            if piece.seek_s > _EPS:
                # Input-side seek: decode from the nearest prior keyframe
                # instead of decoding and discarding the whole head.
                cmd += ['-ss', f'{piece.seek_s:.6f}']
            cmd += ['-i', piece.item.path]
            filters.append(_seg_filter(in_idx, piece.dur_s, canvas, label))
        labels.append(label)
    filters.append(''.join(labels) + f'concat=n={len(pieces)}:v=1:a=0[out]')

    cmd += ['-filter_complex', ';'.join(filters)]
    cmd += ['-map', '[out]', *_encoder_args()]
    cmd += ['-movflags', '+faststart', output_path]
    _run_ffmpeg(cmd)


def _assemble_with_stream_copy(pieces, copyable, canvas, output_path):
    """Fast path: copy whole segments' bitstreams, encode only the rest.

    Every piece becomes an MPEG-TS intermediate next to output_path —
    copyable segments via `-c copy` (a remux, no decode; TS carries the
    SPS/PPS in-band so bitstreams from different encoder configurations
    can be spliced), the others through the same normalisation chain and
    encoder as the single-pass path.  The concat demuxer then joins the
    intermediates with `-c copy` into the final faststart MP4, so the
    copied content is never re-encoded at all.
    """
    workdir = os.path.dirname(output_path)
    ts_paths = []
    for k, piece in enumerate(pieces):
        ts_path = os.path.join(workdir, f'piece_{k:04d}.ts')
        ts_paths.append(ts_path)
        if copyable[k]:
            _run_ffmpeg(['ffmpeg', '-y', '-i', piece.item.path,
                         '-map', '0:v:0', '-c', 'copy',
                         '-bsf:v', 'h264_mp4toannexb',
                         '-f', 'mpegts', ts_path])
            continue
        cmd = ['ffmpeg', '-y']
        if piece.kind == 'gap':
            cmd += ['-filter_complex',
                    _gap_filter(piece.dur_s, canvas, '[out]')]
        else:
            if piece.seek_s > _EPS:
                cmd += ['-ss', f'{piece.seek_s:.6f}']
            cmd += ['-i', piece.item.path]
            cmd += ['-filter_complex',
                    _seg_filter(0, piece.dur_s, canvas, '[out]')]
        cmd += ['-map', '[out]', *_encoder_args(), '-f', 'mpegts', ts_path]
        _run_ffmpeg(cmd)

    list_path = os.path.join(workdir, 'concat.txt')
    with open(list_path, 'w') as fh:
        for ts_path in ts_paths:
            fh.write(f"file '{ts_path}'\n")
    _run_ffmpeg(['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                 '-i', list_path, '-c', 'copy',
                 '-movflags', '+faststart', output_path])


def transcode_to_video(stage_dir, start_ts, end_ts,
                       fill_color_argb, output_path,
                       default_width=1920, default_height=1080,
                       _query_info=None):
    """Assemble a single MP4 covering [start_ts, end_ts] from staged segments.

    The window is cut into consecutive pieces — one clip per overlapping
    segment, one generated fill_color_argb (0xAARRGGBB) clip per uncovered
    stretch.  Whole segments that need no trimming are stream-copied (no
    re-encode); everything else is normalised to a common size/rate/format
    and encoded, and the pieces are joined in order.  When nothing is
    copyable a single-pass concat-filter encode runs instead; it is also
    the fallback if the fast path fails.  The output is written with
    `-movflags +faststart` so web players can start playback before the
    full file has been received.
    output_path will be overwritten.  Raises RuntimeError on ffmpeg failure.
    """
    if _query_info is None:
        _query_info = _query_file_info

    timeline = _build_timeline(stage_dir, start_ts, end_ts)

    width, height, fps_str = default_width, default_height, _DEFAULT_FPS
    for item in timeline:
        _, w, h, fps, _codec = _query_info(item.path)
        if w > 0 and h > 0:
            width, height, fps_str = w, h, fps
            break
    canvas = _Canvas(width, height, fps_str,
                     _fill_color_str(fill_color_argb))

    pieces   = _plan_pieces(timeline, end_ts - start_ts)
    copyable = [_is_stream_copyable(p, canvas, _query_info) for p in pieces]

    if any(copyable):
        try:
            _assemble_with_stream_copy(pieces, copyable, canvas, output_path)
            return
        except RuntimeError as exc:
            print(f'[video] WARNING: stream-copy fast path failed ({exc}); '
                  'falling back to full re-encode', flush=True)
    _encode_single_pass(pieces, canvas, output_path)
