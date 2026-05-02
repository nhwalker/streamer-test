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
            tee_webrtc. -> videocrop (bottom) -> webrtcsink   (top half)
            tee_webrtc. -> videocrop (top)    -> webrtcsink   (bottom half)

Environment variables:
  CASTER_HOST            caster hostname / IP; empty = host mode  ("")
  CASTER_SIGNALLING_PORT caster's signalling server port          (8443)

  DISPLAY                X11 display for host mode                (:0)
  STREAM_WIDTH           capture width for host mode              (1920)
  STREAM_HEIGHT          capture height for host mode             (1080)
  STREAM_FRAMERATE       frames per second for host mode          (30)

  ARCHIVE_DIR            output dir for .mkv segments             (/archive)
  ARCHIVE_SEGMENT_SEC    segment duration in seconds              (600)
  ARCHIVE_BITRATE        archive H.264 bitrate in kbps            (6000)
  ARCHIVE_PREFIX         filename prefix for segments             (stream)
  ARCHIVE_MAX_BYTES      delete oldest segments when total archive size
                         exceeds this many bytes; 0 = unlimited          (0)
  ARCHIVE_MAX_AGE_DAYS   delete segments older than this many days;
                         0 = unlimited                                    (0)

  SIGNALLING_PORT        service's browser-facing signalling port (8443)
                         top-half  stream uses SIGNALLING_PORT+1  (8444)
                         bottom-half stream uses SIGNALLING_PORT+2 (8445)
  CROP_HEIGHT            pixel row where frame is split            (1080)
  GST_WEBRTC_STUN_SERVER optional STUN URI                        ("")
  GST_WEBRTC_TURN_SERVER optional TURN URI                        ("")
"""
import json as _json
import os
import signal
import struct
import sys
import time

import gi
from archive_purge import purge_archive
from archive_times import renamed_segment_path
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
gi.require_version('GstRtp', '1.0')
from gi.repository import Gst, GLib, GstRtp  # noqa: E402 - must follow gi.require_version

CASTER_HOST           = os.environ.get('CASTER_HOST', '')
CASTER_SIG_PORT       = os.environ.get('CASTER_SIGNALLING_PORT', '8443')
CASTER_PEER_ID        = os.environ.get('CASTER_PEER_ID', 'desktop-caster')

HOST_MODE             = not bool(CASTER_HOST)
DISPLAY               = os.environ.get('DISPLAY', ':0')
WIDTH                 = os.environ.get('STREAM_WIDTH', '1920')
HEIGHT                = os.environ.get('STREAM_HEIGHT', '1080')
FRAMERATE             = os.environ.get('STREAM_FRAMERATE', '30')

ARCHIVE_DIR           = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC   = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
ARCHIVE_BITRATE       = int(os.environ.get('ARCHIVE_BITRATE', '6000'))
ARCHIVE_PREFIX        = os.environ.get('ARCHIVE_PREFIX', 'stream')
ARCHIVE_MAX_BYTES     = int(os.environ.get('ARCHIVE_MAX_BYTES', '0'))
ARCHIVE_MAX_AGE_DAYS  = int(os.environ.get('ARCHIVE_MAX_AGE_DAYS', '0'))

SIG_PORT              = os.environ.get('SIGNALLING_PORT', '8443')
SIG_PORT_TOP          = str(int(SIG_PORT) + 1)
SIG_PORT_BOTTOM       = str(int(SIG_PORT) + 2)
CROP_HEIGHT           = int(os.environ.get('CROP_HEIGHT', '1080'))
STUN                  = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN                  = os.environ.get('GST_WEBRTC_TURN_SERVER', '')

WEBRTC_VIDEO_CAPS     = 'video/x-vp9;video/x-h264'

# ── Absolute Capture Time RTP header extension ────────────────────────────────
# Mutable dict so the pad-probe closure and the extension class share state
# without needing globals or nonlocal.
_capture_state = {'ns': 0}

# RTP payloader element factory names that carry video to the browser.
_RTP_PAYLOADER_NAMES  = frozenset({'rtph264pay', 'rtpvp9pay', 'rtpvp8pay', 'rtpav1pay'})

# NTP epoch is 70 years before Unix epoch.
_NTP_UNIX_OFFSET_S = 2_208_988_800


class AbsCaptureTimeExt(GstRtp.RTPHeaderExtension):
    """
    Writes the abs-capture-time RTP header extension (short form, 8 bytes).

    URI: urn:ietf:params:rtp-hdrext:abs-capture-time

    The extension carries the wall-clock capture time as a 64-bit NTP
    timestamp (UQ32.32).  Chrome's requestVideoFrameCallback exposes this as
    metadata.captureTime so the browser can compute true per-frame latency
    without any data-channel side channel.
    """

    __gtype_name__ = 'AbsCaptureTimeExt'
    _URI = 'urn:ietf:params:rtp-hdrext:abs-capture-time'

    def do_get_supported_flags(self):
        return (GstRtp.RTPHeaderExtensionFlags.ONE_BYTE |
                GstRtp.RTPHeaderExtensionFlags.TWO_BYTE)

    def do_get_max_size(self, input_meta):
        return 8  # 64-bit NTP timestamp, short form

    def do_write(self, input_meta, write_flags, output, data):
        ns = _capture_state['ns']
        if ns == 0:
            return 0
        s = ns * 1e-9 + _NTP_UNIX_OFFSET_S
        sec  = int(s)
        frac = int((s - sec) * (1 << 32))
        struct.pack_into('>II', data, 0, sec, frac)
        return 8

    def do_read(self, read_flags, data, buffer):
        return True  # sender-only; receiver side unused


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

    Gst.init(None)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    segment_ns      = ARCHIVE_SEGMENT_SEC * Gst.SECOND
    archive_pattern = os.path.join(ARCHIVE_DIR, f'{ARCHIVE_PREFIX}-%05d.mkv')

    print('[service] Starting stream service:', flush=True)
    if HOST_MODE:
        print(f'  Mode              : host (X11 direct capture)')
        print(f'  Display           : {DISPLAY}')
        print(f'  Resolution        : {WIDTH}x{HEIGHT} @ {FRAMERATE} fps')
    else:
        caster_sig_uri = f'ws://{CASTER_HOST}:{CASTER_SIG_PORT}'
        print(f'  Mode              : caster')
        print(f'  Caster signalling : {caster_sig_uri}')
    print(f'  Archive           : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Archive bitrate   : {ARCHIVE_BITRATE} kbps')
    print(f'  Signalling /      : ws://127.0.0.1:{SIG_PORT}')
    print(f'  Signalling /top   : ws://127.0.0.1:{SIG_PORT_TOP}')
    print(f'  Signalling /bottom: ws://127.0.0.1:{SIG_PORT_BOTTOM}')
    print(f'  Crop height       : {CROP_HEIGHT}px')
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

    # ── Browser WebRTC branch: fan out to full / top / bottom streams
    q_webrtc   = make('queue',       'q_webrtc')
    tee_webrtc = make('tee',         't_webrtc')

    q_full     = make('queue',       'q_full')
    ws_full    = make('webrtcsink',  'ws_full')

    q_top      = make('queue',       'q_top')
    crop_top   = make('videocrop',   'crop_top')
    ws_top     = make('webrtcsink',  'ws_top')

    q_bot      = make('queue',       'q_bot')
    crop_bot   = make('videocrop',   'crop_bot')
    ws_bot     = make('webrtcsink',  'ws_bot')

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
            Gst.Caps.from_string(f'video/x-raw,width={WIDTH},height={HEIGHT}'),
        )
    else:
        wsrc = make('webrtcsrc', 'wsrc')
        wsrc.get_property('signaller').set_property('uri', caster_sig_uri)
        wsrc.get_property('signaller').set_property('producer-peer-id', CASTER_PEER_ID)

    # ── Capture timestamp probe: records wall-clock ns when each frame enters
    # vconvert.  The AbsCaptureTimeExt reads this value and writes it as an NTP
    # timestamp into every outgoing RTP packet so the browser can compute true
    # per-frame service→browser latency via requestVideoFrameCallback.
    def _on_capture_buffer(_pad, _info):
        _capture_state['ns'] = time.time_ns()
        return Gst.PadProbeReturn.OK

    vconvert.get_static_pad('sink').add_probe(
        Gst.PadProbeType.BUFFER, _on_capture_buffer)

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
        src = os.path.join(ARCHIVE_DIR, f'{ARCHIVE_PREFIX}-{frag_id:05d}.mkv')
        dst = renamed_segment_path(src, start_ns, end_ns, ARCHIVE_PREFIX)
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

    # ── Configure videocrop: remove CROP_HEIGHT pixels from the named edge
    crop_top.set_property('bottom', CROP_HEIGHT)   # keep top half
    crop_bot.set_property('top',    CROP_HEIGHT)   # keep bottom half

    # ── Configure browser webrtcsink instances
    for sink, port in ((ws_full, SIG_PORT), (ws_top, SIG_PORT_TOP), (ws_bot, SIG_PORT_BOTTOM)):
        sink.get_property('signaller').set_property('uri', f'ws://127.0.0.1:{port}')
        sink.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))
        if STUN:
            sink.set_property('stun-server', STUN)

    # ── Data-channel relay: service → browser ────────────────────────────────
    # Sends the capture timestamp (ms since Unix epoch) every 200 ms over a
    # WebRTC data channel named 'ts'.  This is the fallback latency signal used
    # by the browser when abs-capture-time is not available via requestVideoFrameCallback.
    _browser_channels = {}

    def _on_browser_consumer_added(_sink, peer_id, webrtcbin):
        ch = webrtcbin.emit('create-data-channel', 'ts',
                            Gst.Structure.new_empty('config'))
        _browser_channels[peer_id] = ch

    def _on_browser_consumer_removed(_sink, peer_id, _webrtcbin):
        _browser_channels.pop(peer_id, None)

    for _s in (ws_full, ws_top, ws_bot):
        _s.connect('consumer-added',   _on_browser_consumer_added)
        _s.connect('consumer-removed', _on_browser_consumer_removed)

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

    tee_webrtc.link(q_top)
    q_top.link(crop_top)
    crop_top.link(ws_top)

    tee_webrtc.link(q_bot)
    q_bot.link(crop_bot)
    crop_bot.link(ws_bot)

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

    # ── deep-element-added: TURN + abs-capture-time extension on payloaders ──
    # Always connected (not only when TURN is set) so payloaders are reached
    # regardless of TURN configuration.
    def on_deep_element_added(_bin, _sub_bin, element):
        factory = element.get_factory()
        if not factory:
            return
        name = factory.get_name()

        if TURN and name == 'webrtcbin':
            try:
                ok = element.emit('add-turn-server', TURN)
                print(f'[service] add-turn-server: '
                      f'{"OK" if ok else "FAILED"}', flush=True)
            except Exception as exc:
                print(f'[service] WARNING: add-turn-server failed: {exc}',
                      file=sys.stderr, flush=True)

        if name in _RTP_PAYLOADER_NAMES:
            ext = AbsCaptureTimeExt()
            ext.set_uri(AbsCaptureTimeExt._URI)
            ext.set_id(3)
            try:
                element.emit('add-extension', ext)
                print(f'[service] abs-capture-time extension attached to {name}',
                      flush=True)
            except Exception as exc:
                print(f'[service] WARNING: add-extension failed on {name}: {exc}',
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

    def _relay_ts():
        ns = _capture_state['ns']
        if _browser_channels and ns:
            msg = _json.dumps({'t': ns // 1_000_000})
            for ch in list(_browser_channels.values()):
                try:
                    ch.emit('send-string', msg)
                except Exception:
                    pass
        return True  # reschedule

    GLib.timeout_add(200, _relay_ts)

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
