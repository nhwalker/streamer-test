"""
video_transcode.py -- ffmpeg-based video assembly for the /video endpoint.

Takes a directory of staged .mkv segments and produces a single output .mkv
that exactly covers the requested time window:
  - a full-duration solid-color base video covers the entire window
  - each segment is overlaid at its correct temporal position
  - segments starting before the window are trimmed; segments extending past
    the end are cut off implicitly when the base video ends
  - the output is always a complete, decodable Matroska file

Requires: ffmpeg and ffprobe available in PATH.

Public API:
    transcode_to_video(stage_dir, start_ts, end_ts, segment_sec,
                       fill_color_argb, output_path,
                       default_width, default_height)
"""
import json
import os
import subprocess
import time
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

def _build_timeline(stage_dir, start_ts, end_ts, segment_sec, _query_info=None):
    """Build an ordered list of TimelineItem covering [start_ts, end_ts].

    Each item represents a file segment that overlaps the window.  Gaps are
    implicit: the base color video in transcode_to_video fills them.

    Segments starting before start_ts have offset_s > 0; their leading
    pre-window content is trimmed by transcode_to_video.  Segments extending
    past end_ts are cut off automatically when the base video ends.

    Returns [] when no segments overlap (pure color output).

    _query_info is injectable for unit testing (defaults to _query_file_info).
    """
    if _query_info is None:
        _query_info = _query_file_info

    all_files = [
        os.path.join(stage_dir, f)
        for f in os.listdir(stage_dir)
        if f.endswith('.mkv')
    ]

    if not all_files:
        return []

    file_info = {path: _query_info(path) for path in all_files}

    renamed = []
    unnamed = []
    for path in all_files:
        times = parse_segment_times(os.path.basename(path))
        if times:
            renamed.append((times[0], path))
        else:
            unnamed.append(path)

    renamed.sort(key=lambda x: x[0])
    unnamed.sort(key=os.path.getmtime)

    segments = [(seg_start, path) for seg_start, path in renamed]

    last_known_end = None
    if renamed:
        last_start, last_path = renamed[-1]
        last_known_end = last_start + file_info[last_path][0]

    for i, path in enumerate(unnamed):
        mtime = os.path.getmtime(path)
        seg_start = (
            last_known_end if i == 0 and last_known_end is not None
            else mtime - segment_sec if i == 0
            else os.path.getmtime(unnamed[i - 1])
        )
        segments.append((seg_start, path))

    segments.sort(key=lambda x: x[0])

    timeline = []
    for seg_start, path in segments:
        dur_s = file_info[path][0]
        if dur_s == 0:
            dur_s = max(0.0, time.time() - seg_start)
        seg_end = seg_start + dur_s

        if seg_start >= end_ts or seg_end <= start_ts:
            continue

        clip_start = max(seg_start, start_ts)
        timeline.append(TimelineItem(
            path           = path,
            offset_s       = clip_start - seg_start,
            output_start_s = clip_start - start_ts,
        ))

    return timeline


# ── ffmpeg assembly ───────────────────────────────────────────────────────────

def transcode_to_video(stage_dir, start_ts, end_ts, segment_sec,
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

    timeline = _build_timeline(
        stage_dir, start_ts, end_ts, segment_sec, _query_info=_query_info)

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

    base_filter = (
        f'color=c={color}:s={size}:r={fps_str}'
        f':duration={total_dur:.6f},setpts=PTS-STARTPTS[base]'
    )
    filters.append(base_filter)

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
        src   = f'[{prev}]' if prev == 'base' else prev
        clip  = f'[c{i}]'
        if i < len(timeline) - 1:
            out = f'[t{i}]'
        else:
            out = '[out]'
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
