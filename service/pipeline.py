#!/usr/bin/env python3
"""
pipeline.py -- desktop-stream-service: RTP ingress -> tee -> archive + WebRTC.

udpsrc (RTP) -> rtpbin -> rtph264depay -> h264parse -> tee
  tee. -> splitmuxsink matroskamux               (archive, H.264 passthrough)
  tee. -> <nvh264dec|avdec_h264> -> webrtcsink   (decode once, per-peer encode)

rtpbin.recv_rtp_src_0_0 is a dynamic pad emitted when the first RTP packet
arrives; a pad-added handler links it to rtph264depay.  The pipeline is
therefore built via the GObject API (no parse_launch) to allow this late link.

The tee sits before the decoder so the archive branch never decodes or
re-encodes -- .mkv files are the caster's H.264 bytes muxed directly
into segmented Matroska containers.  Matroska is streaming-by-default
(no buffer-until-EOS like mp4mux), so the on-disk file is always
readable and a kill -9 mid-segment loses at most one cluster.

The WebRTC branch decodes once; webrtcsink then re-encodes per peer for
adaptive bitrate, preferring nvh264enc by plugin rank when a GPU is
present.

RTCP feedback loop (if DESKTOP_HOST is set):
  udpsrc (RTCP SR from caster) -> rtpbin.recv_rtcp_sink_0
  rtpbin.send_rtcp_src_0       -> udpsink (RTCP RR -> caster)

Environment variables:
  DESKTOP_HOST           caster hostname for RTCP RR (optional)
  RTP_PORT               RTP video port                         (5000)
                         RTCP SR ← caster at RTP_PORT + 1
                         RTCP RR → caster on  RTP_PORT + 2

  ARCHIVE_DIR            output dir for .mkv segments           (/archive)
  ARCHIVE_SEGMENT_SEC    segment duration in seconds            (600)

  SIGNALLING_PORT        signalling server port                 (8443)
  GST_WEBRTC_STUN_SERVER optional STUN URI                      ("")
  GST_WEBRTC_TURN_SERVER optional TURN URI                      ("")
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

DESKTOP_HOST        = os.environ.get('DESKTOP_HOST', '')
RTP_PORT            = os.environ.get('RTP_PORT', '5000')

ARCHIVE_DIR         = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))

SIG_PORT            = os.environ.get('SIGNALLING_PORT', '8443')
STUN                = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN                = os.environ.get('GST_WEBRTC_TURN_SERVER', '')

WEBRTC_VIDEO_CAPS   = 'video/x-vp9;video/x-h264'


def select_decoder():
    """Return the H.264 decoder element name, preferring NVDEC when present."""
    if Gst.ElementFactory.find('nvh264dec'):
        print('[service] NVIDIA NVDEC detected: using nvh264dec', flush=True)
        return 'nvh264dec'
    print('[service] NVIDIA NVDEC not detected: using avdec_h264 (software)',
          flush=True)
    return 'avdec_h264'


def _request_pad(element, name):
    """Request a pad by name, compatible with GStreamer < and >= 1.20."""
    if hasattr(element, 'request_pad_simple'):
        return element.request_pad_simple(name)
    return element.get_request_pad(name)


def main():
    if not DESKTOP_HOST:
        print('[service] WARNING: DESKTOP_HOST not set; '
              'RTCP bitrate feedback to caster disabled.', flush=True)

    Gst.init(None)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    decoder    = select_decoder()
    sig_uri    = f'ws://127.0.0.1:{SIG_PORT}'
    segment_ns = ARCHIVE_SEGMENT_SEC * Gst.SECOND
    archive_pattern = os.path.join(ARCHIVE_DIR, 'stream-%05d.mkv')

    rtp_port = int(RTP_PORT)
    rtcp_sr  = rtp_port + 1   # service listens for SR from caster
    rtcp_rr  = rtp_port + 2   # service sends RR back to caster

    print('[service] Starting stream service:', flush=True)
    print(f'  Caster RTP    : rtp://0.0.0.0:{rtp_port} (listening)')
    print(f'  Archive       : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Signalling    : {sig_uri}')
    print(f'  WebRTC codecs : {WEBRTC_VIDEO_CAPS}')
    if DESKTOP_HOST:
        print(f'  RTCP RR→      : udp://{DESKTOP_HOST}:{rtcp_rr}')
    if STUN:
        print(f'  STUN          : {STUN}')
    if TURN:
        print(f'  TURN          : {TURN}')

    pipeline = Gst.Pipeline.new('service-pipeline')

    def make(kind, name=None):
        el = Gst.ElementFactory.make(kind, name)
        if el is None:
            print(f'[service] ERROR: cannot create element {kind!r}',
                  file=sys.stderr)
            sys.exit(1)
        pipeline.add(el)
        return el

    # ── RTP ingress (dynamic pad from rtpbin requires programmatic linking)
    udpsrc_rtp = make('udpsrc',      'udpsrc_rtp')
    rtpbin     = make('rtpbin',      'rtpbin')
    depay      = make('rtph264depay', 'depay')
    q_rtp      = make('queue',       'q_rtp')
    h264parse  = make('h264parse',   'h264parse')
    tee        = make('tee',         't')

    # ── Archive branch
    q_arch  = make('queue',       'q_arch')
    archive = make('splitmuxsink', 'archive')

    # ── WebRTC branch
    q_webrtc   = make('queue',        'q_webrtc')
    decoder_el = make(decoder,        'decoder')
    vconvert   = make('videoconvert', 'vconvert')
    ws         = make('webrtcsink',   'ws')

    # ── RTCP transport
    udpsrc_rtcp  = make('udpsrc',  'udpsrc_rtcp')
    udpsink_rtcp = make('udpsink', 'udpsink_rtcp')

    # ── Configure elements
    rtp_caps = Gst.Caps.from_string(
        'application/x-rtp,media=video,payload=96,'
        'clock-rate=90000,encoding-name=H264'
    )
    udpsrc_rtp.set_property('port', rtp_port)
    udpsrc_rtp.set_property('caps', rtp_caps)

    rtcp_caps = Gst.Caps.from_string('application/x-rtcp')
    udpsrc_rtcp.set_property('port', rtcp_sr)
    udpsrc_rtcp.set_property('caps', rtcp_caps)

    if DESKTOP_HOST:
        udpsink_rtcp.set_property('host', DESKTOP_HOST)
        udpsink_rtcp.set_property('port', rtcp_rr)
        udpsink_rtcp.set_property('sync', False)
        udpsink_rtcp.set_property('async', False)

    # config-interval=-1: inject SPS/PPS before every keyframe so each
    # splitmuxsink segment is self-contained and independently decodable.
    h264parse.set_property('config-interval', -1)

    archive.set_property('muxer-factory', 'matroskamux')
    archive.set_property('location', archive_pattern)
    archive.set_property('max-size-time', segment_ns)

    # signaller::uri uses GstChildProxy notation which PyGObject's set_property
    # does not support; access the signaller child object directly instead.
    ws.get_property('signaller').set_property('uri', sig_uri)
    if STUN:
        ws.set_property('stun-server', STUN)
    ws.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))

    # ── Link RTP/RTCP request pads (exist at construction time)
    rtp_sink_pad = _request_pad(rtpbin, 'recv_rtp_sink_0')
    udpsrc_rtp.get_static_pad('src').link(rtp_sink_pad)

    rtcp_sink_pad = _request_pad(rtpbin, 'recv_rtcp_sink_0')
    udpsrc_rtcp.get_static_pad('src').link(rtcp_sink_pad)

    if DESKTOP_HOST:
        rtcp_src_pad = _request_pad(rtpbin, 'send_rtcp_src_0')
        rtcp_src_pad.link(udpsink_rtcp.get_static_pad('sink'))

    # ── Static chain: depay -> q_rtp -> h264parse -> tee
    depay.link(q_rtp)
    q_rtp.link(h264parse)
    h264parse.link(tee)

    # ── Archive: tee -> q_arch -> splitmuxsink
    tee.link(q_arch)
    q_arch.link(archive)

    # ── WebRTC: tee -> q_webrtc -> decoder -> vconvert -> webrtcsink
    tee.link(q_webrtc)
    q_webrtc.link(decoder_el)
    decoder_el.link(vconvert)
    vconvert.link(ws)

    # ── rtpbin emits recv_rtp_src_0_0 when the first RTP packet arrives.
    def on_pad_added(_, pad):
        if not pad.get_name().startswith('recv_rtp_src_'):
            return
        sink = depay.get_static_pad('sink')
        if sink.is_linked():
            return
        ret = pad.link(sink)
        if ret != Gst.PadLinkReturn.OK:
            print(f'[service] ERROR: rtpbin pad link failed: {ret}',
                  file=sys.stderr)
        else:
            print('[service] rtpbin → rtph264depay linked', flush=True)

    rtpbin.connect('pad-added', on_pad_added)

    # ── TURN is per-consumer on webrtcsink 0.13.x (no top-level property).
    if TURN:
        def on_deep_element_added(_bin, _sub_bin, element):
            factory = element.get_factory()
            if factory and factory.get_name() == 'webrtcbin':
                print('[service] webrtcbin found -- calling add-turn-server',
                      flush=True)
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
