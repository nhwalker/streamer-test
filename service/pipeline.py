#!/usr/bin/env python3
"""
pipeline.py -- desktop-stream-service runner: ffmpeg capture/encode process
supervision + archive segment finalization.

The heavy lifting lives elsewhere:

  stream_command.py    builds the ffmpeg argv (capture -> live RTSP tiers
                       published to the co-located MediaMTX + archive
                       fragmented-MP4 segments) and the MediaMTX config.
  archive_encoder.py   picks the archive encoder args per ARCHIVE_QUALITY.
  archive_finalize.py  tails ffmpeg's segment list and publishes completed
                       segments into ARCHIVE_DIR under timestamped names.

This module:

  * probes h264_nvenc once (a one-frame test encode) to choose the GPU or
    CPU encoder set for the whole process,
  * spawns the single ffmpeg process and restarts it (with backoff) if it
    dies — MediaMTX tolerates the publisher dropping and re-appearing, so
    a crash costs viewers a few seconds of freeze, not a page reload,
  * runs the SegmentFinalizer poll loop while ffmpeg is alive,
  * forwards SIGTERM/SIGINT as a graceful ffmpeg shutdown (ffmpeg writes
    the final segment-list entry on clean exit, so the last archive
    segment is finalized too),
  * schedules archive purging (ARCHIVE_MAX_BYTES / ARCHIVE_MAX_AGE_DAYS).

Environment variables (see stream_command.py for the LIVE_* /
MEDIAMTX_* / WEBRTC_* knobs, desktop_config.py for geometry/ladder):

  DISPLAY                X11 display                              (:0)
  STREAM_FRAMERATE       frames per second                        (30)

  ARCHIVE_DIR            output dir for completed .mp4 segments   (/archive)
  ARCHIVE_LIVE_DIR       output dir for the in-progress .mp4      (/archive-live)
                         segment; written as fragmented MP4 with
                         the moov atom up front (empty_moov), so it
                         is parseable mid-write, then renamed/moved
                         to ARCHIVE_DIR on rotation
  ARCHIVE_SEGMENT_SEC    segment duration in seconds              (600)
  ARCHIVE_QUALITY        'visually-lossless' | 'lossless' | 'legacy'
                                                                  (visually-lossless)
  ARCHIVE_QP             QP for 'visually-lossless' mode          (18)
  ARCHIVE_BITRATE        kbps, 'legacy' mode only                 (6000)
  ARCHIVE_MAX_BYTES      delete oldest segments when total archive size
                         exceeds this many bytes; 0 = unlimited   (0)
  ARCHIVE_MAX_AGE_DAYS   delete segments older than this many days;
                         0 = unlimited                            (0)
"""
import os
import signal
import subprocess
import sys
import threading
import time

from archive_encoder import archive_encoder_args
from archive_finalize import SegmentFinalizer, next_segment_number
from archive_purge import purge_archive
from desktop_config import load_config
from encoders import nvenc_works
from stream_command import build_ffmpeg_command, live_gop

ARCHIVE_DIR          = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_LIVE_DIR     = os.environ.get('ARCHIVE_LIVE_DIR', '/archive-live')
ARCHIVE_SEGMENT_SEC  = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
ARCHIVE_QUALITY      = os.environ.get('ARCHIVE_QUALITY', 'visually-lossless').strip().lower()
ARCHIVE_QP           = int(os.environ.get('ARCHIVE_QP', '18'))
ARCHIVE_BITRATE      = int(os.environ.get('ARCHIVE_BITRATE', '6000'))
ARCHIVE_MAX_BYTES    = int(os.environ.get('ARCHIVE_MAX_BYTES', '0'))
ARCHIVE_MAX_AGE_DAYS = int(os.environ.get('ARCHIVE_MAX_AGE_DAYS', '0'))

# Supervision: restart ffmpeg after unexpected exits, backing off so a
# hard-broken configuration (bad display, missing encoder) doesn't spin.
_RESTART_BACKOFF_START_SEC = 2.0
_RESTART_BACKOFF_MAX_SEC   = 30.0
# The finalizer re-reads the segment CSV every _FINALIZE_POLL_SEC; segments
# rotate every ARCHIVE_SEGMENT_SEC (minutes), so sub-second polling buys
# nothing.  Don't raise it much further though: until a rotated segment is
# finalized it is invisible to /archive, whose active-segment time estimate
# is anchored on the last *finalized* segment.  The ffmpeg process itself
# is watched at _PROC_TICK_SEC so exits and shutdowns stay prompt.
_FINALIZE_POLL_SEC         = 2.0
_PROC_TICK_SEC             = 0.5
# Graceful-stop budget: SIGINT lets ffmpeg flush the last segment + list
# entry; escalate to SIGKILL if it wedges.
_STOP_GRACE_SEC            = 10.0


def _print_summary(config, use_nvenc, archive_pattern):
    desktop_name = config['desktopName']
    print('[service] Starting stream service:', flush=True)
    print(f'  Desktop name      : {desktop_name}')
    print(f'  Display           : {os.environ.get("DISPLAY", ":0")}')
    print(f'  Resolution        : {config["width"]}x{config["height"]}'
          f' @ {config["framerate"]} fps')
    print(f'  Pipeline mode     : '
          f'{"GPU (h264_nvenc)" if use_nvenc else "CPU (libx264)"}')
    print(f'  Live segments     : {archive_pattern}'
          f' ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Completed archive : {ARCHIVE_DIR}')
    print(f'  Archive quality   : {ARCHIVE_QUALITY}'
          + (f' (QP={ARCHIVE_QP})' if ARCHIVE_QUALITY == 'visually-lossless'
             else f' (legacy bitrate={ARCHIVE_BITRATE}kbps)' if ARCHIVE_QUALITY == 'legacy'
             else ''))
    webrtc_port = config.get('webrtcPort', 8889)
    print('  WHEP endpoints    :')
    for t in config.get('fullTiers', []):
        print(f'    scale={t["scale"]:<5} {t["width"]}x{t["height"]}'
              f'  http://<host>:{webrtc_port}/{t["whepPath"]}/whep')
    for s in config['screens']:
        print(f'    {s["path"]} (region {s["width"]}x{s["height"]}'
              f'+{s["x"]}+{s["y"]}):')
        for t in s.get('tiers', []):
            print(f'      scale={t["scale"]:<5} {t["width"]}x{t["height"]}'
                  f'  http://<host>:{webrtc_port}/{t["whepPath"]}/whep')


def main():
    config = load_config()
    desktop_name = config['desktopName']

    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_LIVE_DIR, exist_ok=True)

    use_nvenc = nvenc_works(require_ffmpeg=True)
    archive_pattern   = os.path.join(ARCHIVE_LIVE_DIR, f'{desktop_name}-%05d.mp4')
    segment_list_path = os.path.join(ARCHIVE_LIVE_DIR, 'segments.csv')

    _print_summary(config, use_nvenc, archive_pattern)

    arch_args = archive_encoder_args(
        quality=ARCHIVE_QUALITY, qp=ARCHIVE_QP,
        bitrate_legacy=ARCHIVE_BITRATE, use_nvenc=use_nvenc,
        gop=live_gop(os.environ),
    )

    shutting_down = threading.Event()
    proc = None   # current ffmpeg; the signal handler closes over it

    def on_signal(sig, _frame):
        print(f'[service] Signal {sig} received, stopping ffmpeg', flush=True)
        shutting_down.set()
        if proc and proc.poll() is None:
            # SIGINT == ffmpeg's own graceful-quit path: it finishes the
            # current segment, writes the trailer + final segment-list
            # entry, and exits.
            proc.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT,  on_signal)

    if ARCHIVE_MAX_BYTES or ARCHIVE_MAX_AGE_DAYS:
        def _purge_loop():
            while not shutting_down.wait(ARCHIVE_SEGMENT_SEC):
                purge_archive(ARCHIVE_DIR, ARCHIVE_MAX_BYTES,
                              ARCHIVE_MAX_AGE_DAYS)
        purge_archive(ARCHIVE_DIR, ARCHIVE_MAX_BYTES, ARCHIVE_MAX_AGE_DAYS)
        threading.Thread(target=_purge_loop, name='archive-purge',
                         daemon=True).start()

    backoff = _RESTART_BACKOFF_START_SEC
    while not shutting_down.is_set():
        # Fresh segment list per ffmpeg run — the finalizer tails from
        # offset 0 and stream time restarts at 0 with a new epoch anchor.
        try:
            os.unlink(segment_list_path)
        except FileNotFoundError:
            pass

        start_number = next_segment_number(ARCHIVE_LIVE_DIR, desktop_name)
        cmd = build_ffmpeg_command(
            config, os.environ, use_nvenc, arch_args,
            archive_pattern, segment_list_path,
            ARCHIVE_SEGMENT_SEC, segment_start_number=start_number,
        )

        epoch_anchor = time.time()
        print(f'[service] launching ffmpeg ({len(cmd)} args, '
              f'segment numbering from {start_number})', flush=True)
        proc = subprocess.Popen(cmd)   # stdout/stderr inherit -> container log

        finalizer = SegmentFinalizer(
            segment_list_path, ARCHIVE_LIVE_DIR, ARCHIVE_DIR,
            desktop_name, epoch_anchor,
        )
        started = time.monotonic()
        while proc.poll() is None:
            finalizer.poll()
            deadline = time.monotonic() + _FINALIZE_POLL_SEC
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(_PROC_TICK_SEC)

        if shutting_down.is_set():
            try:
                proc.wait(timeout=_STOP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                print('[service] WARNING: ffmpeg ignored SIGINT; killing',
                      file=sys.stderr, flush=True)
                proc.kill()
                proc.wait()
        # Final poll picks up the last segment's list entry (written by
        # ffmpeg at clean exit).
        finalizer.poll()

        if shutting_down.is_set():
            break

        ran_for = time.monotonic() - started
        if ran_for > 60:
            backoff = _RESTART_BACKOFF_START_SEC  # it was healthy; reset
        print(f'[service] WARNING: ffmpeg exited (rc={proc.returncode}) '
              f'after {ran_for:.0f}s; restarting in {backoff:.0f}s',
              file=sys.stderr, flush=True)
        if shutting_down.wait(backoff):
            break
        backoff = min(backoff * 2, _RESTART_BACKOFF_MAX_SEC)

    print('[service] Pipeline stopped', flush=True)


if __name__ == '__main__':
    main()
