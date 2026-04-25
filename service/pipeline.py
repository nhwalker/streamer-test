#!/usr/bin/env python3
"""
pipeline.py -- desktop-stream-service: webrtcsrc ingress -> tee -> archive + WebRTC.

webrtcsrc (connects to caster's signalling server) -> videoconvert -> tee
  tee. -> encoder -> h264parse -> splitmuxsink matroskamux    (archive)
  tee. -> webrtcsink                                           (browser WebRTC)

The service's webrtcsrc dials the caster's gst-webrtc-signalling-server.
webrtcsink (service-side) handles per-browser encoding and adaptive bitrate
for viewers automatically; the caster's webrtcsink does the same for the
ingest leg.  No manual RTCP feedback loop is needed.

webrtcsrc has a dynamic src pad; a pad-added handler links the first video
pad to the videoconvert → tee chain.

The archive branch re-encodes from the decoded raw video webrtcsrc provides.
nvh264enc is preferred when a GPU is present; x264enc is the software fallback.

Environment variables:
  CASTER_HOST            caster hostname / IP (required)
  CASTER_SIGNALLING_PORT caster's signalling server port  (8443)

  ARCHIVE_DIR            output dir for .mkv segments     (/archive)
  ARCHIVE_SEGMENT_SEC    segment duration in seconds       (600)
  ARCHIVE_BITRATE        archive H.264 bitrate in kbps     (6000)

  SIGNALLING_PORT        service's own signalling port
                         (browsers connect here)           (8443)
  GST_WEBRTC_STUN_SERVER optional STUN URI                 ("")
  GST_WEBRTC_TURN_SERVER optional TURN URI                 ("")
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

CASTER_HOST           = os.environ.get('CASTER_HOST', '')
CASTER_SIG_PORT       = os.environ.get('CASTER_SIGNALLING_PORT', '8443')
CASTER_PEER_ID        = os.environ.get('CASTER_PEER_ID', 'desktop-caster')

ARCHIVE_DIR           = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC   = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
ARCHIVE_BITRATE       = int(os.environ.get('ARCHIVE_BITRATE', '6000'))

SIG_PORT              = os.environ.get('SIGNALLING_PORT', '8443')
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
    if not CASTER_HOST:
        print('[service] ERROR: CASTER_HOST is required '
              '(IP/hostname of the caster)', file=sys.stderr)
        sys.exit(1)

    Gst.init(None)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    caster_sig_uri  = f'ws://{CASTER_HOST}:{CASTER_SIG_PORT}'
    service_sig_uri = f'ws://127.0.0.1:{SIG_PORT}'
    segment_ns      = ARCHIVE_SEGMENT_SEC * Gst.SECOND
    archive_pattern = os.path.join(ARCHIVE_DIR, 'stream-%05d.mkv')

    print('[service] Starting stream service:', flush=True)
    print(f'  Caster signalling : {caster_sig_uri}')
    print(f'  Archive           : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Archive bitrate   : {ARCHIVE_BITRATE} kbps')
    print(f'  Browser signalling: {service_sig_uri}')
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

    # ── Ingress from caster (dynamic src pad)
    wsrc     = make('webrtcsrc',    'wsrc')
    vconvert = make('videoconvert', 'vconvert')
    tee      = make('tee',          't')

    # ── Archive branch
    q_arch   = make('queue',        'q_arch')
    arch_enc = build_archive_encoder()
    pipeline.add(arch_enc)
    arch_h264 = make('h264parse',   'arch_h264')
    archive   = make('splitmuxsink', 'archive')

    # ── Browser WebRTC branch
    q_webrtc = make('queue',        'q_webrtc')
    ws       = make('webrtcsink',   'ws')

    # ── Configure webrtcsrc (connects to caster's signalling server)
    wsrc.get_property('signaller').set_property('uri', caster_sig_uri)
    wsrc.set_property('producer-peer-id', CASTER_PEER_ID)

    # ── Configure archive
    # config-interval=-1: SPS/PPS before every keyframe → each segment is
    # independently decodable without seeking to the start.
    arch_h264.set_property('config-interval', -1)
    archive.set_property('muxer-factory', 'matroskamux')
    archive.set_property('location', archive_pattern)
    archive.set_property('max-size-time', segment_ns)

    # ── Configure browser webrtcsink
    ws.get_property('signaller').set_property('uri', service_sig_uri)
    ws.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))
    if STUN:
        ws.set_property('stun-server', STUN)

    # ── Static links: vconvert -> tee -> both branches
    vconvert.link(tee)
    tee.link(q_arch)
    q_arch.link(arch_enc)
    arch_enc.link(arch_h264)
    arch_h264.link(archive)
    tee.link(q_webrtc)
    q_webrtc.link(ws)

    # ── Dynamic src pad from webrtcsrc → videoconvert
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

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        print('[service] Pipeline stopped')


if __name__ == '__main__':
    main()
