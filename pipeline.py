#!/usr/bin/env python3
"""
pipeline.py — GStreamer desktop capture pipeline.

Launches ximagesrc → videorate → videoscale → videoconvert → webrtcsink,
and (when GST_WEBRTC_TURN_SERVER is set) attaches a TURN server to each
per-consumer webrtcbin that webrtcsink creates.

Environment variables:
  DISPLAY                X11 display                   (:0)
  STREAM_CODEC           vp9 | vp8 | h264 | h265       (vp9)
  STREAM_WIDTH           capture width                  (1920)
  STREAM_HEIGHT          capture height                 (1080)
  STREAM_FRAMERATE       frames per second              (30)
  SIGNALLING_PORT        signalling server port         (8443)
  GST_WEBRTC_STUN_SERVER optional STUN URI             ("")
  GST_WEBRTC_TURN_SERVER optional TURN URI             ("")
                         Format: turn://user:pass@host:port
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 — must follow gi.require_version

DISPLAY   = os.environ.get('DISPLAY', ':0')
CODEC     = os.environ.get('STREAM_CODEC', 'vp9')
WIDTH     = os.environ.get('STREAM_WIDTH', '1920')
HEIGHT    = os.environ.get('STREAM_HEIGHT', '1080')
FRAMERATE = os.environ.get('STREAM_FRAMERATE', '30')
SIG_PORT  = os.environ.get('SIGNALLING_PORT', '8443')
STUN      = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN      = os.environ.get('GST_WEBRTC_TURN_SERVER', '')

CODEC_CAPS = {
    'vp9':  'video/x-vp9',
    'vp8':  'video/x-vp8',
    'h264': 'video/x-h264',
    'h265': 'video/x-h265',
}


def main():
    Gst.init(None)

    if CODEC not in CODEC_CAPS:
        print(f'[pipeline] ERROR: Unknown STREAM_CODEC "{CODEC}". '
              'Use vp9, vp8, h264, or h265.', file=sys.stderr)
        sys.exit(1)

    caps    = CODEC_CAPS[CODEC]
    sig_uri = f'ws://127.0.0.1:{SIG_PORT}'

    if Gst.ElementFactory.find('nvh264enc'):
        print('[pipeline] NVIDIA NVENC detected: hardware encoding available')
    else:
        print('[pipeline] NVIDIA NVENC not detected: software encoding will be used')

    print('[pipeline] Starting capture:')
    print(f'  Display    : {DISPLAY}')
    print(f'  Resolution : {WIDTH}x{HEIGHT} @ {FRAMERATE} fps')
    print(f'  Codec      : {caps}')
    print(f'  Signalling : {sig_uri}')
    if STUN:
        print(f'  STUN       : {STUN}')
    if TURN:
        print(f'  TURN       : {TURN}')

    desc = (
        f'ximagesrc display-name={DISPLAY} use-damage=false '
        f'! videorate '
        f'! video/x-raw,framerate={FRAMERATE}/1 '
        f'! videoscale '
        f'! video/x-raw,width={WIDTH},height={HEIGHT} '
        f'! videoconvert '
        f'! queue '
        f'! webrtcsink name=ws '
        f'signaller::uri={sig_uri} '
        f'video-caps={caps}'
    )
    if STUN:
        desc += f' stun-server={STUN}'

    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f'[pipeline] ERROR: Failed to parse pipeline: {exc}', file=sys.stderr)
        sys.exit(1)

    # Configure TURN on every webrtcbin that webrtcsink creates.
    # webrtcsink 0.13.x has no webrtcbin-ready signal, but deep-element-added
    # on the pipeline walks the full descendant tree, so it fires for every
    # webrtcbin the sink spawns per consumer before ICE starts.
    if TURN:
        def on_deep_element_added(_bin, _sub_bin, element):
            factory = element.get_factory()
            if factory and factory.get_name() == 'webrtcbin':
                print('[pipeline] webrtcbin found — calling add-turn-server', flush=True)
                try:
                    ok = element.emit('add-turn-server', TURN)
                    print(f'[pipeline] add-turn-server: {"OK" if ok else "FAILED"}',
                          flush=True)
                except Exception as exc:
                    print(f'[pipeline] WARNING: add-turn-server failed: {exc}',
                          file=sys.stderr, flush=True)

        pipeline.connect('deep-element-added', on_deep_element_added)
        print('[pipeline] Listening for webrtcbin creation to configure TURN',
              flush=True)

    loop = GLib.MainLoop()
    bus  = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            print('[pipeline] EOS received')
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f'[pipeline] ERROR: {err}', file=sys.stderr)
            if dbg:
                print(f'[pipeline] debug: {dbg}', file=sys.stderr)
            loop.quit()

    bus.connect('message', on_message)
    pipeline.set_state(Gst.State.PLAYING)

    def on_signal(sig, _frame):
        print(f'[pipeline] Signal {sig} received, sending EOS')
        pipeline.send_event(Gst.Event.new_eos())

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT,  on_signal)

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        print('[pipeline] Pipeline stopped')


if __name__ == '__main__':
    main()
