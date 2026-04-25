#!/usr/bin/env python3
"""
pipeline.py — desktop-caster: X11 capture -> H.264 -> SRT listener.

ximagesrc -> videorate -> videoscale -> videoconvert -> (nvh264enc|x264enc)
 -> h264parse -> mpegtsmux -> srtsink mode=listener

The desktop-stream-service container dials this listener over SRT.  Because
we listen here, the desktop has no knowledge of the server -- the server
is the source of truth for which desktops exist.

Environment variables:
  DISPLAY           X11 display                     (:0)
  STREAM_WIDTH      capture width                   (1920)
  STREAM_HEIGHT     capture height                  (1080)
  STREAM_FRAMERATE  frames per second               (30)
  STREAM_BITRATE    encoder target bitrate in kbps  (6000)
  SRT_PORT          SRT listener port               (9000)
  SRT_LATENCY       SRT buffer in ms                (40)
  SRT_PASSPHRASE    optional AES key (16+ chars)    ("")
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

DISPLAY     = os.environ.get('DISPLAY', ':0')
WIDTH       = os.environ.get('STREAM_WIDTH', '1920')
HEIGHT      = os.environ.get('STREAM_HEIGHT', '1080')
FRAMERATE   = os.environ.get('STREAM_FRAMERATE', '30')
BITRATE     = os.environ.get('STREAM_BITRATE', '6000')
SRT_PORT    = os.environ.get('SRT_PORT', '9000')
SRT_LATENCY = os.environ.get('SRT_LATENCY', '40')
SRT_PASS    = os.environ.get('SRT_PASSPHRASE', '')


def build_encoder():
    """Return a gst-launch encoder fragment, preferring NVENC when present."""
    # 1-second GOP keeps splitmuxsink's segment boundaries tight on the
    # receiving end (it waits for the next keyframe to rotate segments).
    gop = FRAMERATE
    if Gst.ElementFactory.find('nvh264enc'):
        print('[caster] NVIDIA NVENC detected: using nvh264enc', flush=True)
        return (
            f'nvh264enc preset=low-latency-hq rc-mode=cbr '
            f'bitrate={BITRATE} gop-size={gop}'
        )
    print('[caster] NVIDIA NVENC not detected: using x264enc (software)',
          flush=True)
    return (
        f'x264enc tune=zerolatency speed-preset=ultrafast '
        f'bitrate={BITRATE} key-int-max={gop}'
    )


def build_srt_uri():
    params = [f'mode=listener', f'latency={SRT_LATENCY}']
    if SRT_PASS:
        # pbkeylen must be 16, 24, or 32; pick the smallest that accepts the key.
        params.append(f'passphrase={SRT_PASS}')
        params.append('pbkeylen=16')
    return f'srt://:{SRT_PORT}?' + '&'.join(params)


def main():
    Gst.init(None)

    encoder = build_encoder()
    srt_uri = build_srt_uri()

    print('[caster] Starting capture:', flush=True)
    print(f'  Display    : {DISPLAY}')
    print(f'  Resolution : {WIDTH}x{HEIGHT} @ {FRAMERATE} fps')
    print(f'  Bitrate    : {BITRATE} kbps')
    print(f'  SRT URI    : srt://0.0.0.0:{SRT_PORT} (listener)')
    if SRT_PASS:
        print(f'  SRT        : AES-128 encrypted')

    desc = (
        f'ximagesrc display-name={DISPLAY} use-damage=false '
        f'! videorate '
        f'! video/x-raw,framerate={FRAMERATE}/1 '
        f'! videoscale '
        f'! video/x-raw,width={WIDTH},height={HEIGHT} '
        f'! videoconvert '
        f'! {encoder} '
        f'! h264parse config-interval=1 '
        # Force Annex-B byte-stream with AU alignment going into srtsink so
        # the receiver can demux it with h264parse alone (no mpegtsmux +
        # tsdemux dance, which introduces a dynamic-pad link that fails
        # silently when tsdemux never exposes a video pad).
        f'! video/x-h264,stream-format=byte-stream,alignment=au '
        f'! srtsink uri="{srt_uri}" wait-for-connection=false'
    )

    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f'[caster] ERROR: Failed to parse pipeline: {exc}',
              file=sys.stderr)
        sys.exit(1)

    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
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
