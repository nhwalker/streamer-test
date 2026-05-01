"""
video_transcode.py -- GStreamer-based video assembly for the /video endpoint.

Takes a directory of staged .mkv segments and produces a single output .mkv
that exactly covers the requested time window:
  - segments are clipped to the window boundaries
  - gaps (missing coverage) are filled with solid-color frames
  - the output is always a complete, decodable Matroska file

Public API:
    transcode_to_video(stage_dir, start_ts, end_ts, segment_sec,
                       fill_color_argb, output_path,
                       default_width, default_height)
"""
import os
import time
from dataclasses import dataclass
from typing import Optional

from archive_times import parse_segment_times

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GLib', '2.0')
    from gi.repository import Gst, GLib
    Gst.init(None)
    _GST_AVAILABLE = True
except Exception:
    Gst = None
    GLib = None
    _GST_AVAILABLE = False

_DEFAULT_FPS_NUM   = 25
_DEFAULT_FPS_DENOM = 1


@dataclass
class TimelineItem:
    start_ts:    float          # wall-clock epoch seconds
    end_ts:      float          # wall-clock epoch seconds
    path:        Optional[str]  # None → gap (fill color)
    offset_ns:   int            # seek position within file (0 for gaps)
    duration_ns: int            # how long this item lasts in the output


# ── File introspection ────────────────────────────────────────────────────────

def _query_file_info(path):
    """Return (duration_ns, width, height, fps_num, fps_denom) for a .mkv file.

    Uses a playbin pipeline set to PAUSED so no frames are decoded.
    Falls back to zeroed duration if the file is truncated/unfinished.
    """
    pipe = Gst.ElementFactory.make('playbin', None)
    vsink = Gst.ElementFactory.make('appsink', None)
    asink = Gst.ElementFactory.make('fakesink', None)
    vsink.set_property('sync', False)
    vsink.set_property('max-buffers', 1)
    vsink.set_property('drop', True)
    pipe.set_property('uri', Gst.filename_to_uri(path))
    pipe.set_property('video-sink', vsink)
    pipe.set_property('audio-sink', asink)

    pipe.set_state(Gst.State.PAUSED)
    pipe.get_state(Gst.CLOCK_TIME_NONE)

    ok, duration_ns = pipe.query_duration(Gst.Format.TIME)
    if not ok or duration_ns == Gst.CLOCK_TIME_NONE:
        duration_ns = 0

    width = height = 0
    fps_num, fps_denom = _DEFAULT_FPS_NUM, _DEFAULT_FPS_DENOM
    pad = vsink.get_static_pad('sink')
    if pad:
        caps = pad.get_current_caps()
        if caps and caps.get_size() > 0:
            s = caps.get_structure(0)
            _, w = s.get_int('width')
            _, h = s.get_int('height')
            _, fn, fd = s.get_fraction('framerate')
            if w > 0:
                width = w
            if h > 0:
                height = h
            if fn > 0 and fd > 0:
                fps_num, fps_denom = fn, fd

    pipe.set_state(Gst.State.NULL)
    return duration_ns, width, height, fps_num, fps_denom


# ── Timeline builder ──────────────────────────────────────────────────────────

def _build_timeline(stage_dir, start_ts, end_ts, segment_sec,
                    _query_info=None):
    """Build an ordered list of TimelineItem covering [start_ts, end_ts].

    Segments in stage_dir are classified as renamed (timestamps in filename)
    or unnamed (mtime-based start estimate), mirroring the logic in
    stage_segments().  Duration for each is queried from the file itself —
    the end timestamp embedded in renamed filenames is deliberately ignored
    so that the clip boundaries are driven by actual recorded content.

    Gaps between (and before/after) segments are filled with None-path items.

    _query_info is injectable for unit testing (defaults to _query_file_info).
    """
    if _query_info is None:
        _query_info = _query_file_info

    all_files = [
        os.path.join(stage_dir, f)
        for f in os.listdir(stage_dir)
        if f.endswith('.mkv')
    ]

    segments = []  # [(seg_start_ts, path)]

    if all_files:
        renamed  = []
        unnamed  = []
        for path in all_files:
            times = parse_segment_times(os.path.basename(path))
            if times:
                renamed.append((times[0], path))
            else:
                unnamed.append(path)

        renamed.sort(key=lambda x: x[0])
        unnamed.sort(key=os.path.getmtime)

        for seg_start, path in renamed:
            segments.append((seg_start, path))

        last_known_end = None
        if renamed:
            # Estimate last_known_end from the last renamed segment
            last_start, last_path = renamed[-1]
            dur_ns, *_ = _query_info(last_path)
            last_known_end = last_start + dur_ns / 1e9

        for i, path in enumerate(unnamed):
            mtime = os.path.getmtime(path)
            if i == 0:
                seg_start = last_known_end if last_known_end is not None \
                            else mtime - segment_sec
            else:
                seg_start = os.path.getmtime(unnamed[i - 1])
            segments.append((seg_start, path))

        segments.sort(key=lambda x: x[0])

    # Build timeline items with real durations
    raw = []  # [(seg_start_ts, seg_end_ts, path)]
    for seg_start, path in segments:
        dur_ns, *_ = _query_info(path)
        if dur_ns == 0:
            # Unfinished active segment: estimate duration as time since start
            dur_ns = max(0, int((time.time() - seg_start) * 1e9))
        seg_end = seg_start + dur_ns / 1e9
        # Only keep if it overlaps [start_ts, end_ts]
        if seg_start < end_ts and seg_end > start_ts:
            raw.append((seg_start, seg_end, path))

    # Build final timeline: gaps + clipped file items covering [start_ts, end_ts]
    timeline = []
    cursor = start_ts

    for seg_start, seg_end, path in raw:
        # Gap before this segment
        gap_end = min(seg_start, end_ts)
        if cursor < gap_end:
            gap_dur_ns = int((gap_end - cursor) * 1e9)
            timeline.append(TimelineItem(
                start_ts=cursor, end_ts=gap_end,
                path=None, offset_ns=0, duration_ns=gap_dur_ns,
            ))
        if seg_start >= end_ts:
            break
        cursor = max(cursor, seg_start)

        # Clipped file item
        clip_start = cursor
        clip_end   = min(seg_end, end_ts)
        offset_ns  = int((clip_start - seg_start) * 1e9)
        dur_ns     = int((clip_end - clip_start) * 1e9)
        if dur_ns > 0:
            timeline.append(TimelineItem(
                start_ts=clip_start, end_ts=clip_end,
                path=path, offset_ns=offset_ns, duration_ns=dur_ns,
            ))
        cursor = clip_end

    # Trailing gap
    if cursor < end_ts:
        gap_dur_ns = int((end_ts - cursor) * 1e9)
        timeline.append(TimelineItem(
            start_ts=cursor, end_ts=end_ts,
            path=None, offset_ns=0, duration_ns=gap_dur_ns,
        ))

    return timeline


# ── Fill-frame generator ──────────────────────────────────────────────────────

def _argb_to_i420_planes(color_argb, width, height):
    """Return (y_byte, cb_byte, cr_byte) for a solid I420 frame."""
    r = (color_argb >> 16) & 0xFF
    g = (color_argb >>  8) & 0xFF
    b =  color_argb        & 0xFF
    # BT.601 full-range
    y  = max(0, min(255, int( 0.299 * r + 0.587 * g + 0.114 * b)))
    cb = max(0, min(255, int(-0.169 * r - 0.331 * g + 0.500 * b + 128)))
    cr = max(0, min(255, int( 0.500 * r - 0.419 * g - 0.081 * b + 128)))
    return y, cb, cr


def _make_fill_frame(color_argb, width, height):
    """Return a packed I420 frame bytes for a solid color."""
    y, cb, cr = _argb_to_i420_planes(color_argb, width, height)
    y_plane  = bytes([y])  * (width * height)
    uv_size  = (width // 2) * (height // 2)
    cb_plane = bytes([cb]) * uv_size
    cr_plane = bytes([cr]) * uv_size
    return y_plane + cb_plane + cr_plane


# ── Clip extraction ───────────────────────────────────────────────────────────

def _push_clip(path, offset_ns, duration_ns, appsrc,
               current_pts, frame_dur_ns, width, height, fps_num, fps_denom):
    """Decode a clipped portion of path and push raw frames to appsrc.

    Returns the updated current_pts after all pushed frames.
    """
    caps_str = (
        f'video/x-raw,format=I420,width={width},height={height},'
        f'framerate={fps_num}/{fps_denom}'
    )
    pipe = Gst.ElementFactory.make('playbin', None)
    vsink = Gst.ElementFactory.make('appsink', None)
    asink = Gst.ElementFactory.make('fakesink', None)
    vsink.set_property('caps', Gst.Caps.from_string(caps_str))
    vsink.set_property('sync', False)
    vsink.set_property('max-buffers', 8)
    vsink.set_property('drop', False)
    pipe.set_property('uri', Gst.filename_to_uri(path))
    pipe.set_property('video-sink', vsink)
    pipe.set_property('audio-sink', asink)

    pipe.set_state(Gst.State.PAUSED)
    pipe.get_state(Gst.CLOCK_TIME_NONE)

    if offset_ns > 0:
        pipe.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            offset_ns,
        )
        pipe.get_state(Gst.CLOCK_TIME_NONE)

    pipe.set_state(Gst.State.PLAYING)

    clip_end_in_file_ns = offset_ns + duration_ns
    bus = pipe.get_bus()

    while True:
        sample = vsink.try_pull_sample(500 * Gst.MSECOND)
        if sample is None:
            msg = bus.timed_pop_filtered(
                0, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if msg:
                if msg.type == Gst.MessageType.ERROR:
                    err, _ = msg.parse_error()
                    raise RuntimeError(f'GStreamer decode error: {err}')
                break
            continue

        buf = sample.get_buffer()
        pts = buf.pts
        if pts != Gst.CLOCK_TIME_NONE and pts >= clip_end_in_file_ns:
            break

        copy = buf.copy()
        copy.pts      = current_pts
        copy.dts      = current_pts
        copy.duration = frame_dur_ns
        ret = appsrc.emit('push-buffer', copy)
        if ret != Gst.FlowReturn.OK:
            break
        current_pts += frame_dur_ns

    pipe.set_state(Gst.State.NULL)
    return current_pts


# ── Main entry point ──────────────────────────────────────────────────────────

def transcode_to_video(stage_dir, start_ts, end_ts, segment_sec,
                       fill_color_argb, output_path,
                       default_width=1920, default_height=1080,
                       _query_info=None):
    """Assemble a single MKV covering [start_ts, end_ts] from staged segments.

    Gaps are filled with solid frames of fill_color_argb (0xAARRGGBB).
    output_path will be overwritten.  Raises RuntimeError on pipeline failure.
    """
    if _query_info is None:
        _query_info = _query_file_info

    timeline = _build_timeline(
        stage_dir, start_ts, end_ts, segment_sec, _query_info=_query_info)

    # Determine output video properties from the first file clip, or defaults
    width, height = default_width, default_height
    fps_num, fps_denom = _DEFAULT_FPS_NUM, _DEFAULT_FPS_DENOM
    for item in timeline:
        if item.path is not None:
            dur_ns, w, h, fn, fd = _query_info(item.path)
            if w > 0 and h > 0:
                width, height = w, h
            if fn > 0 and fd > 0:
                fps_num, fps_denom = fn, fd
            break

    frame_dur_ns  = Gst.SECOND * fps_denom // fps_num
    fill_frame    = _make_fill_frame(fill_color_argb, width, height)
    caps_str      = (
        f'video/x-raw,format=I420,width={width},height={height},'
        f'framerate={fps_num}/{fps_denom}'
    )

    # Build encode pipeline
    enc_pipe = Gst.Pipeline.new('video-enc')

    def _make(kind, name=None):
        el = Gst.ElementFactory.make(kind, name)
        if el is None:
            raise RuntimeError(f'Cannot create GStreamer element {kind!r}')
        enc_pipe.add(el)
        return el

    appsrc   = _make('appsrc',     'src')
    vconv    = _make('videoconvert')
    encoder  = _make('x264enc')
    h264p    = _make('h264parse')
    mux      = _make('matroskamux')
    filesink = _make('filesink')

    appsrc.set_property('caps',     Gst.Caps.from_string(caps_str))
    appsrc.set_property('format',   Gst.Format.TIME)
    appsrc.set_property('block',    True)
    appsrc.set_property('is-live',  False)
    encoder.set_property('tune',         0x4)  # zerolatency
    encoder.set_property('speed-preset', 1)    # ultrafast
    filesink.set_property('location', output_path)

    appsrc.link(vconv)
    vconv.link(encoder)
    encoder.link(h264p)
    h264p.link(mux)
    mux.link(filesink)

    enc_pipe.set_state(Gst.State.PLAYING)

    current_pts = 0

    for item in timeline:
        if item.path is None:
            # Gap: push solid-color frames
            n_frames = max(1, round(item.duration_ns / frame_dur_ns))
            buf_bytes = fill_frame
            for _ in range(n_frames):
                buf = Gst.Buffer.new_allocate(None, len(buf_bytes), None)
                buf.fill(0, buf_bytes)
                buf.pts      = current_pts
                buf.dts      = current_pts
                buf.duration = frame_dur_ns
                ret = appsrc.emit('push-buffer', buf)
                if ret != Gst.FlowReturn.OK:
                    break
                current_pts += frame_dur_ns
        else:
            current_pts = _push_clip(
                item.path, item.offset_ns, item.duration_ns,
                appsrc, current_pts, frame_dur_ns,
                width, height, fps_num, fps_denom,
            )

    appsrc.emit('end-of-stream')

    # Wait for EOS or error
    bus = enc_pipe.get_bus()
    while True:
        msg = bus.timed_pop_filtered(
            Gst.CLOCK_TIME_NONE,
            Gst.MessageType.EOS | Gst.MessageType.ERROR,
        )
        if msg.type == Gst.MessageType.ERROR:
            err, _ = msg.parse_error()
            enc_pipe.set_state(Gst.State.NULL)
            raise RuntimeError(f'GStreamer encode error: {err}')
        break  # EOS

    enc_pipe.set_state(Gst.State.NULL)
