"""
video_transcode.py -- ffmpeg-based video assembly for the /video endpoint.

Takes a directory of staged .mkv segments and produces a single output .mkv
that exactly covers the requested time window:
  - a full-duration solid-color base video covers the entire window
  - each segment is overlaid at its correct temporal position
  - segments starting before the window are trimmed; segments extending past
    the end are cut off implicitly when the base video ends
  - the output is always a complete, decodable Matroska file

All files in the stage directory must have timestamps in their filenames
(as produced by stage_segments in web_server.py).  Files without recognized
timestamps are ignored.

Requires: ffmpeg and ffprobe available in PATH.

Public API:
    transcode_to_video(stage_dir, start_ts, end_ts,
                       fill_color_argb, output_path,
                       default_width, default_height)
"""
import json
import os
import subprocess
from dataclasses import dataclass

from archive_times import parse_segment_times

_DEFAULT_FPS = '25/1'


@dataclass
class TimelineItem:
    path:           str
    offset_s:       float  # > 0 → trim=start=offset_s to skip pre-window content
    output_start_s: float  # seconds from start of output where overlay begins


# ── File introspection ────────────────────────────────────────────────────────

def _query_file_info(path):
    """Return (duration_s, width, height, fps_str) via ffprobe."""
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-show_format', path],
        capture_output=True, text=True, timeout=10,
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
        if not fname.endswith('.mkv'):
            continue
        times = parse_segment_times(fname)
        if times is None:
            continue
        seg_start, seg_end = times
        if seg_start >= end_ts or seg_end <= start_ts:
            continue
        clip_start = max(seg_start, start_ts)
        timeline.append(TimelineItem(
            path           = os.path.join(stage_dir, fname),
            offset_s       = clip_start - seg_start,
            output_start_s = clip_start - start_ts,
        ))
    timeline.sort(key=lambda x: x.output_start_s)
    return timeline


# ── ffmpeg assembly ───────────────────────────────────────────────────────────

def transcode_to_video(stage_dir, start_ts, end_ts,
                       fill_color_argb, output_path,
                       default_width=1920, default_height=1080,
                       _query_info=None):
    """Assemble a single MKV covering [start_ts, end_ts] from staged segments.

    A solid fill_color_argb (0xAARRGGBB) base video covers the full duration.
    Each segment is overlaid at its correct temporal position.  Gaps between
    segments and at the edges are filled by the base.
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

    cmd = ['ffmpeg', '-y']
    for item in timeline:
        cmd += ['-i', item.path]

    filters = []

    filters.append(
        f'color=c={color}:s={size}:r={fps_str}'
        f':duration={total_dur:.6f},setpts=PTS-STARTPTS[base]'
    )

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
        filters.append(f'{src}{clip}overlay=eof_action=pass{out}')
        prev = f'[t{i}]'

    if not timeline:
        filters.append(
            f'color=c={color}:s={size}:r={fps_str}'
            f':duration={total_dur:.6f},setpts=PTS-STARTPTS[out]'
        )

    cmd += ['-filter_complex', ';'.join(filters)]
    cmd += ['-map', '[out]', '-c:v', 'libx264', '-preset', 'ultrafast', output_path]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f'ffmpeg failed (exit {result.returncode}): '
            f'{result.stderr.decode(errors="replace")[-1000:]}'
        )
