#!/usr/bin/env python3
"""
pipeline.py — desktop-caster: X11 capture -> H.264 -> RTP push.

ximagesrc -> videorate -> videoscale -> videoconvert -> (nvh264enc|x264enc)
 -> h264parse -> rtph264pay -> rtpbin -> udpsink (RTP video)

RTCP feedback loop:
  rtpbin.send_rtcp_src_0 -> udpsink  (RTCP Sender Reports  → service)
  udpsrc                 -> rtpbin   (RTCP Receiver Reports ← service)

Each RTCP RR carries rb-fractionlost (0–255).  On receipt the encoder
bitrate is adjusted: back off 25 % on >5 % loss, probe up 5 % on clean.

Environment variables:
  DISPLAY           X11 display                          (:0)
  STREAM_WIDTH      capture width                        (1920)
  STREAM_HEIGHT     capture height                       (1080)
  STREAM_FRAMERATE  frames per second                    (30)
  STREAM_BITRATE    encoder ceiling in kbps              (6000)
  MIN_BITRATE       encoder floor in kbps                (1000)
  SERVICE_HOST      service hostname/IP (required)
  RTP_PORT          RTP video port                       (5000)
                    RTCP SR  -> service at RTP_PORT + 1
                    RTCP RR  <- service on  RTP_PORT + 2
"""
import os
import signal
import sys

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib  # noqa: E402 - must follow gi.require_version

DISPLAY      = os.environ.get('DISPLAY', ':0')
WIDTH        = os.environ.get('STREAM_WIDTH', '1920')
HEIGHT       = os.environ.get('STREAM_HEIGHT', '1080')
FRAMERATE    = os.environ.get('STREAM_FRAMERATE', '30')
BITRATE      = os.environ.get('STREAM_BITRATE', '6000')
MIN_BITRATE  = os.environ.get('MIN_BITRATE', '1000')
SERVICE_HOST = os.environ.get('SERVICE_HOST', '')
RTP_PORT     = os.environ.get('RTP_PORT', '5000')


def build_encoder():
    """Return a gst-launch encoder fragment; encoder is named 'enc'."""
    # 1-second GOP keeps splitmuxsink segment boundaries tight on the service.
    gop = FRAMERATE
    if Gst.ElementFactory.find('nvh264enc'):
        print('[caster] NVIDIA NVENC detected: using nvh264enc', flush=True)
        return (
            f'nvh264enc name=enc preset=low-latency-hq rc-mode=vbr-hq '
            f'bitrate={MIN_BITRATE} max-bitrate={BITRATE} gop-size={gop}'
        )
    print('[caster] NVIDIA NVENC not detected: using x264enc (software)',
          flush=True)
    # x264enc bitrate is not hot-settable in all builds; RTCP-driven adjustments
    # are logged but may not take effect until the pipeline restarts.
    return (
        f'x264enc name=enc tune=zerolatency speed-preset=ultrafast '
        f'bitrate={BITRATE} key-int-max={gop}'
    )


def connect_rtcp_feedback(rtpbin, encoder):
    """Wire RTCP Receiver Reports to encoder bitrate adjustments."""
    current = [int(BITRATE)]

    def on_ssrc_active(session, ssrc):
        src = session.emit('get-source-by-ssrc', ssrc)
        if src is None:
            return
        stats = src.get_property('stats')
        lost = stats.get_int('rb-fractionlost')[1]   # 0–255 (255 = 100 % loss)
        if lost > 12:          # > ~5 % loss: back off fast
            current[0] = max(int(MIN_BITRATE), int(current[0] * 0.75))
        elif lost == 0:        # clean path: probe up slowly
            current[0] = min(int(BITRATE), int(current[0] * 1.05))
        encoder.set_property('bitrate', current[0])
        print(f'[caster] RTCP: loss={lost}/255 → bitrate={current[0]} kbps',
              flush=True)

    def on_new_ssrc(rb, session_id, ssrc):
        session = rb.emit('get-internal-session', session_id)
        session.connect('on-ssrc-active', on_ssrc_active)

    rtpbin.connect('on-new-ssrc', on_new_ssrc)


def main():
    if not SERVICE_HOST:
        print('[caster] ERROR: SERVICE_HOST is required '
              '(IP/hostname of the service)', file=sys.stderr)
        sys.exit(1)

    Gst.init(None)

    encoder_str = build_encoder()

    rtp_port = int(RTP_PORT)
    rtcp_sr  = rtp_port + 1   # caster sends SR here  (service listens)
    rtcp_rr  = rtp_port + 2   # caster listens for RR (service sends)

    print('[caster] Starting capture:', flush=True)
    print(f'  Display       : {DISPLAY}')
    print(f'  Resolution    : {WIDTH}x{HEIGHT} @ {FRAMERATE} fps')
    print(f'  Bitrate range : {MIN_BITRATE}–{BITRATE} kbps')
    print(f'  Service RTP   : rtp://{SERVICE_HOST}:{rtp_port}')
    print(f'  RTCP SR→      : udp://{SERVICE_HOST}:{rtcp_sr}')
    print(f'  RTCP RR←      : udp://0.0.0.0:{rtcp_rr}')

    desc = (
        f'ximagesrc display-name={DISPLAY} use-damage=false '
        f'! videorate '
        f'! video/x-raw,framerate={FRAMERATE}/1 '
        f'! videoscale '
        f'! video/x-raw,width={WIDTH},height={HEIGHT} '
        f'! videoconvert '
        f'! {encoder_str} '
        f'! h264parse config-interval=1 '
        f'! rtph264pay config-interval=1 pt=96 '
        f'! rtpbin.send_rtp_sink_0 '
        f'rtpbin name=rtpbin '
        f'rtpbin.send_rtp_src_0 '
        f'  ! queue max-size-time=200000000 leaky=downstream '
        f'  ! udpsink host={SERVICE_HOST} port={rtp_port} '
        f'rtpbin.send_rtcp_src_0 '
        f'  ! udpsink host={SERVICE_HOST} port={rtcp_sr} sync=false async=false '
        f'udpsrc port={rtcp_rr} caps="application/x-rtcp" '
        f'  ! rtpbin.recv_rtcp_sink_0'
    )

    try:
        pipeline = Gst.parse_launch(desc)
    except Exception as exc:
        print(f'[caster] ERROR: Failed to parse pipeline: {exc}',
              file=sys.stderr)
        sys.exit(1)

    encoder  = pipeline.get_by_name('enc')
    rtpbin_e = pipeline.get_by_name('rtpbin')
    connect_rtcp_feedback(rtpbin_e, encoder)

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
