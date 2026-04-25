#!/usr/bin/env python3
"""
pipeline.py -- desktop-stream-service: SRT ingress -> tee -> archive + WebRTC.

srtsrc mode=caller -> typefind -> h264parse -> tee
  tee. -> splitmuxsink mp4mux                    (archive, H.264 passthrough)
  tee. -> <nvh264dec|avdec_h264> -> webrtcsink   (decode once, per-peer encode)

The tee sits before the decoder so the archive branch never decodes or
re-encodes -- MP4 files are the caster's H.264 bytes muxed directly into
segmented .mp4 files.  The WebRTC branch decodes once; webrtcsink then
re-encodes per peer for adaptive bitrate, preferring nvh264enc by plugin
rank when a GPU is present.

Environment variables:
  DESKTOP_HOST           caster hostname (required)
  DESKTOP_PORT           caster SRT port                       (9000)
  SRT_LATENCY            SRT buffer in ms                      (40)
  SRT_PASSPHRASE         optional AES key (must match caster)  ("")

  ARCHIVE_DIR            output dir for MP4 segments           (/archive)
  ARCHIVE_SEGMENT_SEC    segment duration in seconds           (600)

  SIGNALLING_PORT        signalling server port                (8443)
  GST_WEBRTC_STUN_SERVER optional STUN URI                     ("")
  GST_WEBRTC_TURN_SERVER optional TURN URI                     ("")
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

DESKTOP_HOST        = os.environ.get('DESKTOP_HOST', '')
DESKTOP_PORT        = os.environ.get('DESKTOP_PORT', '9000')
SRT_LATENCY         = os.environ.get('SRT_LATENCY', '40')
SRT_PASS            = os.environ.get('SRT_PASSPHRASE', '')

ARCHIVE_DIR         = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_SEGMENT_SEC = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))

SIG_PORT            = os.environ.get('SIGNALLING_PORT', '8443')
STUN                = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN                = os.environ.get('GST_WEBRTC_TURN_SERVER', '')

WEBRTC_VIDEO_CAPS   = 'video/x-vp9;video/x-h264'


def build_srt_uri():
    params = [
        'mode=caller',
        f'latency={SRT_LATENCY}',
    ]
    if SRT_PASS:
        params.append(f'passphrase={SRT_PASS}')
        params.append('pbkeylen=16')
    return f'srt://{DESKTOP_HOST}:{DESKTOP_PORT}?' + '&'.join(params)


def select_decoder():
    """Return the H.264 decoder element name, preferring NVDEC when present."""
    if Gst.ElementFactory.find('nvh264dec'):
        print('[service] NVIDIA NVDEC detected: using nvh264dec', flush=True)
        return 'nvh264dec'
    print('[service] NVIDIA NVDEC not detected: using avdec_h264 (software)',
          flush=True)
    return 'avdec_h264'


def main():
    if not DESKTOP_HOST:
        print('[service] ERROR: DESKTOP_HOST is required', file=sys.stderr)
        sys.exit(1)

    Gst.init(None)

    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    srt_uri   = build_srt_uri()
    decoder   = select_decoder()
    sig_uri   = f'ws://127.0.0.1:{SIG_PORT}'
    # splitmuxsink max-size-time is in nanoseconds.
    segment_ns = ARCHIVE_SEGMENT_SEC * Gst.SECOND
    archive_pattern = os.path.join(ARCHIVE_DIR, 'stream-%05d.mp4')

    print('[service] Starting stream service:', flush=True)
    print(f'  Caster        : srt://{DESKTOP_HOST}:{DESKTOP_PORT} (caller)')
    print(f'  Archive       : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Signalling    : {sig_uri}')
    print(f'  WebRTC codecs : {WEBRTC_VIDEO_CAPS}')
    if STUN:
        print(f'  STUN          : {STUN}')
    if TURN:
        print(f'  TURN          : {TURN}')

    # Build the pipeline string.  video-caps is set after parse_launch as a
    # GstCaps object to avoid semicolon-in-property-string escaping issues.
    webrtcsink_frag = f'webrtcsink name=ws signaller::uri={sig_uri}'
    if STUN:
        webrtcsink_frag += f' stun-server={STUN}'

    desc = (
        f'srtsrc uri="{srt_uri}" name=srtsrc '
        # srtsrc has no `caps` property in this gst-plugins-bad build, and
        # h264parse's sink pad refuses ANY caps, so we use typefind to
        # auto-detect the H.264 byte-stream from the data and emit the
        # right caps downstream.  GStreamer's H.264 typefinder recognises
        # Annex-B NAL units and yields video/x-h264,stream-format=byte-
        # stream once enough bytes have flowed.
        f'! typefind '
        f'! queue '
        f'! h264parse config-interval=1 '
        f'! tee name=t '
        f't. ! queue '
        # No muxer-factory= here: we attach a pre-configured mp4mux instance
        # via the `muxer` property below so we can set fragment-duration on
        # it directly (the prior muxer-properties approach silently produced
        # a 0-byte file -- we couldn't tell whether the property was applied
        # or whether mp4mux ignored it).
        f'   ! splitmuxsink name=archive '
        f'     location="{archive_pattern}" '
        f'     max-size-time={segment_ns} '
        f't. ! queue '
        f'   ! {decoder} '
        f'   ! videoconvert '
        f'   ! {webrtcsink_frag}'
    )

    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f'[service] ERROR: Failed to parse pipeline: {exc}',
              file=sys.stderr)
        sys.exit(1)

    # Set video-caps on webrtcsink as a proper GstCaps object (Gst.Caps
    # handles the ; separator cleanly; gst-launch string escaping doesn't).
    ws = pipeline.get_by_name('ws')
    ws.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))

    # Enable fragmented MP4 on the archive muxer.  Non-fragmented mp4mux
    # buffers ftyp/moov until EOS, which leaves in-progress segment files
    # unreadable (0 bytes until splitmuxsink rotates).  Fragmented mode
    # emits moof+mdat every fragment-duration milliseconds, so the file on
    # disk always starts with ftyp+moov and grows continuously -- a kill
    # -9 mid-segment loses at most one fragment.  Playback compat is
    # unaffected (browsers, ffprobe, VLC all handle fMP4; it's the same
    # wire format as DASH/HLS).
    #
    # We construct mp4mux ourselves and pass it via splitmuxsink's `muxer`
    # property rather than relying on muxer-factory + muxer-properties --
    # this lets us readback-verify fragment-duration in the log so we know
    # for certain whether the property took.
    mp4mux = Gst.ElementFactory.make('mp4mux', None)
    if mp4mux is None:
        print('[service] ERROR: failed to create mp4mux element',
              file=sys.stderr)
        sys.exit(1)
    mp4mux.set_property('fragment-duration', 1000)
    print(f'[service] mp4mux fragment-duration='
          f'{mp4mux.get_property("fragment-duration")}', flush=True)

    archive = pipeline.get_by_name('archive')
    archive.set_property('muxer', mp4mux)

    # TURN is per-consumer on webrtcsink 0.13.x (no top-level property).
    # Same pattern as the legacy pipeline: hook deep-element-added and call
    # add-turn-server on every webrtcbin the sink spawns.
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
            # Only log top-level pipeline state transitions; element-level
            # changes are too noisy.  Confirms we actually reach PLAYING.
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
