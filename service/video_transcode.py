"""
video_transcode.py -- ffmpeg-based video assembly for the /video endpoint.

Takes a directory of staged .mkv segments and produces a single output .mkv
that exactly covers the requested time window:
  - segments are clipped to the window boundaries
  - gaps (missing coverage) are filled with solid-color frames
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
from typing import Optional

from archive_times import parse_segment_times

_DEFAULT_FPS = '25/1'


@dataclass
class TimelineItem:
    start_ts:   float          # wall-clock epoch seconds
    end_ts:     float          # wall-clock epoch seconds
    path:       Optional[str]  # None → gap (fill color)
    offset_s:   float          # seek position within file in seconds (0 for gaps)
    duration_s: float          # duration in the output in seconds


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

    Segments are classified as renamed (timestamps in filename) or unnamed
    (mtime-based start estimate), mirroring the logic in stage_segments().
    Duration for each comes from ffprobe — the end timestamp embedded in
    renamed filenames is deliberately ignored so clip boundaries are driven
    by actual recorded content.

    Gaps between and around segments are inserted as None-path items.

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
        return [TimelineItem(start_ts, end_ts, None, 0.0, end_ts - start_ts)]

    # Query every file exactly once
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

    # Resolve real durations and filter to [start_ts, end_ts]
    raw = []
    for seg_start, path in segments:
        dur_s = file_info[path][0]
        if dur_s == 0:
            dur_s = max(0.0, time.time() - seg_start)
        seg_end = seg_start + dur_s
        if seg_start < end_ts and seg_end > start_ts:
            raw.append((seg_start, seg_end, path))

    # Assemble timeline: gaps + clipped file items
    timeline = []
    cursor = start_ts

    for seg_start, seg_end, path in raw:
        gap_end = min(seg_start, end_ts)
        if cursor < gap_end:
            timeline.append(TimelineItem(cursor, gap_end, None, 0.0, gap_end - cursor))
        if seg_start >= end_ts:
            break
        cursor = max(cursor, seg_start)
        clip_end   = min(seg_end, end_ts)
        offset_s   = cursor - seg_start
        duration_s = clip_end - cursor
        if duration_s > 0:
            timeline.append(TimelineItem(cursor, clip_end, path, offset_s, duration_s))
        cursor = clip_end

    if cursor < end_ts:
        timeline.append(TimelineItem(cursor, end_ts, None, 0.0, end_ts - cursor))

    return timeline


# ── ffmpeg assembly ───────────────────────────────────────────────────────────

def transcode_to_video(stage_dir, start_ts, end_ts, segment_sec,
                       fill_color_argb, output_path,
                       default_width=1920, default_height=1080,
                       _query_info=None):
    """Assemble a single MKV covering [start_ts, end_ts] from staged segments.

    Gaps are filled with solid frames of fill_color_argb (0xAARRGGBB).
    output_path will be overwritten.  Raises RuntimeError on ffmpeg failure.
    """
    if _query_info is None:
        _query_info = _query_file_info

    timeline = _build_timeline(
        stage_dir, start_ts, end_ts, segment_sec, _query_info=_query_info)

    # Video properties from first file clip; fall back to defaults
    width, height, fps_str = default_width, default_height, _DEFAULT_FPS
    for item in timeline:
        if item.path is not None:
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
        if item.path is not None:
            cmd += ['-i', item.path]

    filters  = []
    labels   = []
    file_idx = 0
    for i, item in enumerate(timeline):
        label = f'[v{i}]'
        if item.path is None:
            filters.append(
                f'color=c={color}:s={size}:r={fps_str}'
                f':duration={item.duration_s:.6f},setpts=PTS-STARTPTS{label}'
            )
        else:
            filters.append(
                f'[{file_idx}:v]trim=start={item.offset_s:.6f}'
                f':duration={item.duration_s:.6f},setpts=PTS-STARTPTS{label}'
            )
            file_idx += 1
        labels.append(label)

    filters.append(f'{"".join(labels)}concat=n={len(timeline)}:v=1:a=0[out]')
    cmd += ['-filter_complex', ';'.join(filters)]
    cmd += ['-map', '[out]', '-c:v', 'libx264', '-preset', 'ultrafast', output_path]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f'ffmpeg failed (exit {result.returncode}): '
            f'{result.stderr.decode(errors="replace")[-1000:]}'
        )
