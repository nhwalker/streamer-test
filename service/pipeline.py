#!/usr/bin/env python3
"""
pipeline.py -- desktop-stream-service: ingress -> tee -> archive + WebRTC.

Two ingress modes, selected by CASTER_HOST:

  Caster mode (CASTER_HOST set):
    webrtcsrc (connects to caster's signalling server) -> videoconvert -> tee

  Host mode (CASTER_HOST empty):
    ximagesrc -> videorate -> videoscale -> videoconvert -> tee

Either way, the tee fans out identically:
  tee. -> encoder -> h264parse -> splitmuxsink matroskamux    (archive)
  tee. -> tee_webrtc
            tee_webrtc. -> webrtcsink (full)                  (browser, per-peer encode)
            tee_webrtc. -> videocrop (region 0) -> webrtcsink (screen 0)
            tee_webrtc. -> videocrop (region 1) -> webrtcsink (screen 1)
            ...

The resolution and the list of per-screen regions come from
desktop_config.load_config(); see desktop_config.py for the env-var contract.

Environment variables:
  CASTER_HOST            caster hostname / IP; empty = host mode  ("")
  CASTER_SIGNALLING_PORT caster's signalling server port          (8443)

  DISPLAY                X11 display for host mode                (:0)
  STREAM_FRAMERATE       frames per second for host mode          (30)

  ARCHIVE_DIR            output dir for .mkv segments             (/archive)
  ARCHIVE_SEGMENT_SEC    segment duration in seconds              (600)
  ARCHIVE_BITRATE        archive H.264 bitrate in kbps            (6000)
  ARCHIVE_MAX_BYTES      delete oldest segments when total archive size
                         exceeds this many bytes; 0 = unlimited          (0)
  ARCHIVE_MAX_AGE_DAYS   delete segments older than this many days;
                         0 = unlimited                                    (0)

  GST_WEBRTC_STUN_SERVER optional STUN URI                        ("")
  GST_WEBRTC_TURN_SERVER optional TURN URI                        ("")

See desktop_config.py for DESKTOP_NAME, STREAM_WIDTH, STREAM_HEIGHT,
DESKTOP_SPLITS, CROP_HEIGHT, and SIGNALLING_PORT.
"""
import os
import signal
import sys
import time

import gi
from archive_purge import purge_archive
from archive_times import renamed_segment_path
from desktop_config import load_config
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

CASTER_HOST           = os.environ.get('CASTER_HOST', '')
CASTER_SIG_PORT       = os.environ.get('CASTER_SIGNALLING_PORT', '8443')
CASTER_PEER_ID        = os.environ.get('CASTER_PEER_ID', 'desktop-caster')

HOST_MODE             = not bool(CASTER_HOST)
DISPLAY               = os.environ.get('DISPLAY', ':0')
FRAMERATE             = os.environ.get('STREAM_FRAMERATE', '30')

ARCHIVE_DIR           = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC   = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
ARCHIVE_BITRATE       = int(os.environ.get('ARCHIVE_BITRATE', '6000'))
ARCHIVE_MAX_BYTES     = int(os.environ.get('ARCHIVE_MAX_BYTES', '0'))
ARCHIVE_MAX_AGE_DAYS  = int(os.environ.get('ARCHIVE_MAX_AGE_DAYS', '0'))

STUN                  = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN                  = os.environ.get('GST_WEBRTC_TURN_SERVER', '')

WEBRTC_VIDEO_CAPS     = 'video/x-vp9;video/x-h264'


def build_archive_encoder():
    """Return (factory_name, element) for the archive H.264 encoder."""
    if Gst.ElementFactory.find('nvh264enc'):
        print('[service] NVIDIA NVENC detected: using nvh264enc for archive',
              flush=True)
        enc = Gst.ElementFactory.make('nvh264enc', 'arch_enc')
        enc.set_property('preset', 'low-latency-hq')
        enc.set_property('rc-mode', 'vbr-hq')
        enc.set_property('bitrate', ARCHIVE_BITRATE)
        enc.set_property('max-bitrate', ARCHIVE_BITRATE)
        enc.set_property('gop-size', 30)
        return enc
    print('[service] NVIDIA NVENC not detected: using x264enc for archive',
          flush=True)
    enc = Gst.ElementFactory.make('x264enc', 'arch_enc')
    enc.set_property('tune', 0x4)        # zerolatency
    enc.set_property('speed-preset', 1)  # ultrafast
    enc.set_property('bitrate', ARCHIVE_BITRATE)
    enc.set_property('key-int-max', 30)
    return enc


def main():
    if not HOST_MODE and not CASTER_HOST:
        # Shouldn't be reached given HOST_MODE logic, but guard anyway.
        print('[service] ERROR: CASTER_HOST is required in caster mode',
              file=sys.stderr)
        sys.exit(1)

    config         = load_config()
    desktop_name   = config['desktopName']
    width          = config['width']
    height         = config['height']
    full_sig_port  = config['fullSignallingPort']
    screens        = config['screens']
    archive_prefix = desktop_name

    Gst.init(None)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    segment_ns      = ARCHIVE_SEGMENT_SEC * Gst.SECOND
    archive_pattern = os.path.join(ARCHIVE_DIR, f'{archive_prefix}-%05d.mkv')

    print('[service] Starting stream service:', flush=True)
    print(f'  Desktop name      : {desktop_name}')
    if HOST_MODE:
        print(f'  Mode              : host (X11 direct capture)')
        print(f'  Display           : {DISPLAY}')
        print(f'  Resolution        : {width}x{height} @ {FRAMERATE} fps')
    else:
        caster_sig_uri = f'ws://{CASTER_HOST}:{CASTER_SIG_PORT}'
        print(f'  Mode              : caster')
        print(f'  Caster signalling : {caster_sig_uri}')
        print(f'  Resolution        : {width}x{height}')
    print(f'  Archive           : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Archive bitrate   : {ARCHIVE_BITRATE} kbps')
    print(f'  Signalling /      : ws://127.0.0.1:{full_sig_port}')
    for s in screens:
        print(f'  Signalling {s["path"]:<7s}: ws://127.0.0.1:{s["signallingPort"]}'
              f'  region {s["width"]}x{s["height"]}+{s["x"]}+{s["y"]}')
    print(f'  WebRTC codecs     : {WEBRTC_VIDEO_CAPS}')
    if STUN:
        print(f'  STUN              : {STUN}')
    if TURN:
        print(f'  TURN              : {TURN}')

    pipeline = Gst.Pipeline.new('service-pipeline')

    def make(kind, name=None):
        el = Gst.ElementFactory.make(kind, name)
        if el is None:
            print(f'[service] ERROR: cannot create element {kind!r}',
                  file=sys.stderr)
            sys.exit(1)
        pipeline.add(el)
        return el

    # ── Ingress source + videoconvert + tee (mode-dependent)
    vconvert  = make('videoconvert', 'vconvert')
    tee       = make('tee',          't')

    # ── Archive branch
    q_arch    = make('queue',        'q_arch')
    arch_enc  = build_archive_encoder()
    pipeline.add(arch_enc)
    arch_h264 = make('h264parse',    'arch_h264')
    archive   = make('splitmuxsink', 'archive')

    # ── Browser WebRTC branch: fan out to full / per-screen streams
    q_webrtc   = make('queue',       'q_webrtc')
    tee_webrtc = make('tee',         't_webrtc')

    q_full     = make('queue',       'q_full')
    ws_full    = make('webrtcsink',  'ws_full')

    # One queue + videocrop + webrtcsink per configured screen.
    screen_branches = []
    for i, s in enumerate(screens):
        q   = make('queue',      f'q_{s["name"]}')
        vc  = make('videocrop',  f'crop_{s["name"]}')
        wsk = make('webrtcsink', f'ws_{s["name"]}')
        screen_branches.append((s, q, vc, wsk))

    # ── Configure ingress source
    if HOST_MODE:
        xsrc      = make('ximagesrc',   'xsrc')
        vrate     = make('videorate',   'vrate')
        vscale    = make('videoscale',  'vscale')
        xsrc.set_property('display-name', DISPLAY)
        xsrc.set_property('use-damage', False)
        xsrc.link_filtered(
            vrate,
            Gst.Caps.from_string(f'video/x-raw,framerate={FRAMERATE}/1'),
        )
        vrate.link(vscale)
        vscale.link_filtered(
            vconvert,
            Gst.Caps.from_string(f'video/x-raw,width={width},height={height}'),
        )
    else:
        wsrc = make('webrtcsrc', 'wsrc')
        wsrc.get_property('signaller').set_property('uri', caster_sig_uri)
        wsrc.get_property('signaller').set_property('producer-peer-id', CASTER_PEER_ID)

    # ── Configure archive
    # config-interval=-1: SPS/PPS before every keyframe → each segment is
    # independently decodable without seeking to the start.
    arch_h264.set_property('config-interval', -1)
    archive.set_property('muxer-factory', 'matroskamux')
    archive.set_property('location', archive_pattern)
    archive.set_property('max-size-time', segment_ns)

    # ── Rename completed segments to embed their recording timestamps
    # GStreamer's internal clock and Python's time.time() may use different
    # reference points (host CLOCK_MONOTONIC vs container-scoped monotonic).
    # Avoid the mismatch entirely by stamping segments with time.time() at
    # callback invocation.  The end of one fragment == start of the next
    # because both reads happen in the same callback call.
    _fragment_starts = {}  # fragment_id -> start nanoseconds (Unix wall-clock)

    def _rename_fragment(frag_id, end_ns):
        if frag_id not in _fragment_starts:
            return
        start_ns = _fragment_starts.pop(frag_id)
        src = os.path.join(ARCHIVE_DIR, f'{archive_prefix}-{frag_id:05d}.mkv')
        dst = renamed_segment_path(src, start_ns, end_ns, archive_prefix)
        try:
            os.rename(src, dst)
            print(f'[service] archive: {os.path.basename(src)}'
                  f' -> {os.path.basename(dst)}', flush=True)
        except OSError as exc:
            print(f'[service] WARNING: could not rename {src}: {exc}',
                  file=sys.stderr, flush=True)

    def _on_format_location_full(_splitmux, fragment_id, first_sample):
        now_ns = time.time_ns()
        _rename_fragment(fragment_id - 1, now_ns)
        _fragment_starts[fragment_id] = now_ns
        return None

    archive.connect('format-location-full', _on_format_location_full)
    print('[service] archive: using format-location-full (PTS-based timestamps)',
          flush=True)

    # ── Configure videocrop per screen: keep the named region by trimming
    # the four edges around it.  videocrop properties trim from each edge
    # in raw-frame coordinates.
    for s, _q, vc, _wsk in screen_branches:
        right_trim  = max(0, width  - s['x'] - s['width'])
        bottom_trim = max(0, height - s['y'] - s['height'])
        vc.set_property('left',   max(0, s['x']))
        vc.set_property('top',    max(0, s['y']))
        vc.set_property('right',  right_trim)
        vc.set_property('bottom', bottom_trim)

    # ── Configure browser webrtcsink instances
    sinks = [(ws_full, full_sig_port)]
    sinks.extend((wsk, s['signallingPort']) for s, _q, _vc, wsk in screen_branches)
    for sink, port in sinks:
        sink.get_property('signaller').set_property('uri', f'ws://127.0.0.1:{port}')
        sink.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))
        if STUN:
            sink.set_property('stun-server', STUN)

    # ── Static links: vconvert -> tee -> archive + webrtc branches
    vconvert.link(tee)  # host mode: xsrc→vrate→vscale→vconvert already linked above

    tee.link(q_arch)
    q_arch.link(arch_enc)
    arch_enc.link(arch_h264)
    # Explicitly negotiate AVCC (length-prefixed) format so matroskamux writes
    # SPS/PPS in CodecPrivate and NALUs as length-prefixed — without this,
    # some GStreamer versions negotiate byte-stream (Annex B) format, which
    # lands in an AVCC-container without proper CodecPrivate, breaking ffprobe
    # and ffmpeg decoding of the archived segments.
    arch_h264.link_filtered(
        archive,
        Gst.Caps.from_string('video/x-h264, stream-format=avc, alignment=au'),
    )

    tee.link(q_webrtc)
    q_webrtc.link(tee_webrtc)

    tee_webrtc.link(q_full)
    q_full.link(ws_full)

    for _s, q, vc, wsk in screen_branches:
        tee_webrtc.link(q)
        q.link(vc)
        vc.link(wsk)

    # ── Dynamic src pad from webrtcsrc → videoconvert (caster mode only)
    if not HOST_MODE:
        vconvert_sink = vconvert.get_static_pad('sink')

        def on_pad_added(_, pad):
            if pad.get_direction() != Gst.PadDirection.SRC:
                return
            caps_str = pad.query_caps(None).to_string()
            if 'video' not in caps_str:
                return
            if vconvert_sink.is_linked():
                return
            ret = pad.link(vconvert_sink)
            if ret != Gst.PadLinkReturn.OK:
                print(f'[service] ERROR: webrtcsrc pad link failed: {ret}',
                      file=sys.stderr)
            else:
                print('[service] webrtcsrc → videoconvert linked', flush=True)

        wsrc.connect('pad-added', on_pad_added)

    # ── TURN: injected per-webrtcbin instance when it is created
    if TURN:
        def on_deep_element_added(_bin, _sub_bin, element):
            factory = element.get_factory()
            if not factory or factory.get_name() != 'webrtcbin':
                return
            try:
                ok = element.emit('add-turn-server', TURN)
                print(f'[service] add-turn-server: '
                      f'{"OK" if ok else "FAILED"}', flush=True)
            except Exception as exc:
                print(f'[service] WARNING: add-turn-server failed: {exc}',
                      file=sys.stderr, flush=True)

        pipeline.connect('deep-element-added', on_deep_element_added)

    loop = GLib.MainLoop()
    bus  = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            print('[service] EOS received')
            # Rename the last fragment — format-location-full won't fire for it.
            if _fragment_starts:
                end_ns = time.time_ns()
                _rename_fragment(max(_fragment_starts), end_ns)
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f'[service] ERROR: {err}', file=sys.stderr)
            if dbg:
                print(f'[service] debug: {dbg}', file=sys.stderr)
            loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src is pipeline:
            old, new, _ = msg.parse_state_changed()
            print(f'[service] pipeline state: {old.value_nick} -> '
                  f'{new.value_nick}', flush=True)

    bus.connect('message', on_message)
    ret = pipeline.set_state(Gst.State.PLAYING)
    print(f'[service] set_state(PLAYING) returned: {ret.value_nick}',
          flush=True)
    if ret == Gst.StateChangeReturn.FAILURE:
        print('[service] ERROR: pipeline failed to enter PLAYING state',
              file=sys.stderr)
        sys.exit(1)

    def on_signal(sig, _frame):
        print(f'[service] Signal {sig} received, sending EOS')
        pipeline.send_event(Gst.Event.new_eos())

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT,  on_signal)

    if ARCHIVE_MAX_BYTES or ARCHIVE_MAX_AGE_DAYS:
        purge_archive(ARCHIVE_DIR, ARCHIVE_MAX_BYTES, ARCHIVE_MAX_AGE_DAYS)

        def _purge_tick():
            purge_archive(ARCHIVE_DIR, ARCHIVE_MAX_BYTES, ARCHIVE_MAX_AGE_DAYS)
            return True  # reschedule

        GLib.timeout_add_seconds(ARCHIVE_SEGMENT_SEC, _purge_tick)

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        print('[service] Pipeline stopped')


if __name__ == '__main__':
    main()
