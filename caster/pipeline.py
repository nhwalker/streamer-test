#!/usr/bin/env python3
"""
pipeline.py — desktop-caster: X11 capture -> webrtcsink (WHEP-style producer).

ximagesrc -> videorate -> videoscale -> videoconvert -> webrtcsink

webrtcsink publishes to the local gst-webrtc-signalling-server started by
entrypoint.sh.  The service's webrtcsrc connects to the same signalling server
and receives the stream.  webrtcsink handles H.264 encoding (nvh264enc when a
GPU is present, x264enc otherwise) and all RTCP/REMB-based bitrate adaptation
automatically — no custom feedback loop is needed.

Environment variables:
  DISPLAY              X11 display                              (:0)
  STREAM_WIDTH         capture width                            (1920)
  STREAM_HEIGHT        capture height                           (1080)
  STREAM_FRAMERATE     frames per second                        (30)
  SIGNALLING_PORT      port of the local signalling server      (8443)
  GST_WEBRTC_STUN_SERVER  optional STUN URI                    ("")
  GST_WEBRTC_TURN_SERVER  optional TURN URI                    ("")
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

DISPLAY    = os.environ.get('DISPLAY', ':0')
WIDTH      = os.environ.get('STREAM_WIDTH', '1920')
HEIGHT     = os.environ.get('STREAM_HEIGHT', '1080')
FRAMERATE  = os.environ.get('STREAM_FRAMERATE', '30')
SIG_PORT   = os.environ.get('SIGNALLING_PORT', '8443')
STUN       = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN       = os.environ.get('GST_WEBRTC_TURN_SERVER', '')


def main():
    Gst.init(None)

    sig_uri = f'ws://127.0.0.1:{SIG_PORT}'

    print('[caster] Starting capture:', flush=True)
    print(f'  Display     : {DISPLAY}')
    print(f'  Resolution  : {WIDTH}x{HEIGHT} @ {FRAMERATE} fps')
    print(f'  Signalling  : {sig_uri}')
    if STUN:
        print(f'  STUN        : {STUN}')
    if TURN:
        print(f'  TURN        : {TURN}')

    desc = (
        f'ximagesrc display-name={DISPLAY} use-damage=false '
        f'! videorate '
        f'! video/x-raw,framerate={FRAMERATE}/1 '
        f'! videoscale '
        f'! video/x-raw,width={WIDTH},height={HEIGHT} '
        f'! videoconvert '
        f'! webrtcsink name=ws video-caps="video/x-h264"'
    )

    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f'[caster] ERROR: Failed to parse pipeline: {exc}',
              file=sys.stderr)
        sys.exit(1)

    ws = pipeline.get_by_name('ws')
    ws.get_property('signaller').set_property('uri', sig_uri)
    if STUN:
        ws.set_property('stun-server', STUN)

    if TURN:
        def on_deep_element_added(_bin, _sub_bin, element):
            factory = element.get_factory()
            if not factory or factory.get_name() != 'webrtcbin':
                return
            try:
                element.emit('add-turn-server', TURN)
            except Exception as exc:
                print(f'[caster] WARNING: add-turn-server failed: {exc}',
                      file=sys.stderr, flush=True)

        pipeline.connect('deep-element-added', on_deep_element_added)

    loop = GLib.MainLoop()
    bus  = pipeline.get_bus()
    bus.add_signal_watch()

    def on_message(_, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            print('[caster] EOS received')
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print(f'[caster] ERROR: {err}', file=sys.stderr)
            if dbg:
                print(f'[caster] debug: {dbg}', file=sys.stderr)
            loop.quit()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src is pipeline:
            old, new, _ = msg.parse_state_changed()
            print(f'[caster] pipeline state: {old.value_nick} -> '
                  f'{new.value_nick}', flush=True)

    bus.connect('message', on_message)
    ret = pipeline.set_state(Gst.State.PLAYING)
    print(f'[caster] set_state(PLAYING) returned: {ret.value_nick}',
          flush=True)
    if ret == Gst.StateChangeReturn.FAILURE:
        print('[caster] ERROR: pipeline failed to enter PLAYING state',
              file=sys.stderr)
        sys.exit(1)

    def on_signal(sig, _frame):
        print(f'[caster] Signal {sig} received, sending EOS')
        pipeline.send_event(Gst.Event.new_eos())

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT,  on_signal)

    try:
        loop.run()
    finally:
        pipeline.set_state(Gst.State.NULL)
        print('[caster] Pipeline stopped')


if __name__ == '__main__':
    main()
