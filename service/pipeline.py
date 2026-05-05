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

Archive segments are written as Matroska in ARCHIVE_LIVE_DIR (so the
in-progress fragment is readable mid-write), then remuxed to MP4 with
`+faststart` and moved into ARCHIVE_DIR when the fragment rotates.  The
remux is `ffmpeg -c copy` — no re-encoding — and runs on a single
background worker so the GStreamer streaming thread is never blocked.
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

  ARCHIVE_DIR            output dir for completed .mp4 segments   (/archive)
                         (faststart MP4, web-player friendly)
  ARCHIVE_LIVE_DIR       output dir for the in-progress .mkv      (/archive-live)
                         segment; each segment is remuxed to MP4
                         and moved to ARCHIVE_DIR when it rotates
  ARCHIVE_SEGMENT_SEC    segment duration in seconds              (600)
  ARCHIVE_QUALITY        quality mode for the archive H.264 encoder.
                         One of:                                  (visually-lossless)
                           'visually-lossless'  CRF/CQP at ARCHIVE_QP — looks
                                                indistinguishable from source
                                                on screen content; ~2-4× the
                                                file size of the legacy mode.
                           'lossless'           true lossless (QP=0).  Files
                                                can be very large during heavy
                                                motion (50-200 Mbps); set
                                                ARCHIVE_MAX_BYTES.
                           'legacy'             the pre-tuning behaviour:
                                                fixed-bitrate VBR at
                                                ARCHIVE_BITRATE kbps with
                                                latency-tuned presets.
  ARCHIVE_QP             integer 0-51, QP for 'visually-lossless' mode.  Lower
                         is better; QP 18 is the conventional visually-
                         lossless threshold for H.264.  Ignored in 'lossless'
                         and 'legacy' modes.                              (18)
  ARCHIVE_BITRATE_CAP    kbps ceiling on instantaneous bitrate in CQP modes
                         so a chaotic frame can't blow up segment size.
                         100000 = 100 Mbps.                          (100000)
  ARCHIVE_BITRATE        archive H.264 bitrate in kbps; used only by
                         ARCHIVE_QUALITY=legacy.                       (6000)
  ARCHIVE_QUEUE_MAX_BYTES bytes of raw video the q_arch queue may buffer
                         before dropping oldest frames.  At 1080p30 YUV420
                         (~93 MB/s) the default absorbs ~5.5s of encoder
                         lag.  Set to 0 to disable the byte gate (combine
                         with ARCHIVE_QUEUE_MAX_SEC=0 for the legacy
                         unbounded behaviour).             (536870912 = 512 MB)
  ARCHIVE_QUEUE_MAX_SEC  seconds of running-time the q_arch queue may
                         buffer before dropping oldest frames.  Set to 0
                         to disable the time gate.                       (5)
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
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

import gi
from archive_encoder import archive_encoder_plan
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

ARCHIVE_DIR             = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_LIVE_DIR        = os.environ.get('ARCHIVE_LIVE_DIR', '/archive-live')
ARCHIVE_SEGMENT_SEC     = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
ARCHIVE_QUALITY         = os.environ.get('ARCHIVE_QUALITY', 'visually-lossless').strip().lower()
ARCHIVE_QP              = int(os.environ.get('ARCHIVE_QP', '18'))
ARCHIVE_BITRATE_CAP     = int(os.environ.get('ARCHIVE_BITRATE_CAP', '100000'))   # kbps
ARCHIVE_BITRATE         = int(os.environ.get('ARCHIVE_BITRATE', '6000'))         # kbps, legacy mode
ARCHIVE_QUEUE_MAX_BYTES = int(os.environ.get('ARCHIVE_QUEUE_MAX_BYTES', str(512 * 1024 * 1024)))
ARCHIVE_QUEUE_MAX_SEC   = int(os.environ.get('ARCHIVE_QUEUE_MAX_SEC',   '5'))
ARCHIVE_MAX_BYTES       = int(os.environ.get('ARCHIVE_MAX_BYTES', '0'))
ARCHIVE_MAX_AGE_DAYS    = int(os.environ.get('ARCHIVE_MAX_AGE_DAYS', '0'))

STUN                  = os.environ.get('GST_WEBRTC_STUN_SERVER', '')
TURN                  = os.environ.get('GST_WEBRTC_TURN_SERVER', '')

# Per-peer bitrate window for browser-facing webrtcsink instances.  These are
# the bounds REMB / transport-cc is allowed to drive the encoder between; the
# encoder targets whatever the network estimator currently reports inside this
# window.  The ceiling is set high enough that a fat link can carry visually-
# lossless (and occasionally true-lossless) 1080p30 — the encoder will never
# spend it on static content, only burst into it when motion demands.
WEBRTC_MIN_BITRATE   = int(os.environ.get('WEBRTC_MIN_BITRATE',   '500000'))    #   0.5 Mbps
WEBRTC_START_BITRATE = int(os.environ.get('WEBRTC_START_BITRATE', '10000000'))  #  10   Mbps
WEBRTC_MAX_BITRATE   = int(os.environ.get('WEBRTC_MAX_BITRATE',   '80000000'))  #  80   Mbps

WEBRTC_VIDEO_CAPS     = 'video/x-vp9;video/x-h264'


def _set_if_present(element, name, value):
    """Set element[name] = value when the property exists; ignore otherwise.

    The encoders fronted by webrtcsink vary between hosts (NVENC vs. x264 vs.
    libvpx vs. rav1e) and across GStreamer versions.  We probe rather than
    branch hard so a missing property on one build doesn't crash the pipeline.
    """
    try:
        if element.find_property(name) is not None:
            element.set_property(name, value)
    except Exception:
        pass


def _on_encoder_setup(_sink, _consumer_id, _pad_name, encoder):
    """Allow the per-peer encoder to reach lossless / near-lossless quality.

    `webrtcsink` instantiates a fresh encoder per consumer and emits this
    signal so we can override its defaults.  All we do here is widen the QP
    floor down to 0 — without that, x264enc's default `qp-min=10` mathematically
    forbids true lossless output even when REMB tells the encoder it has the
    bandwidth to spend.  Latency-affecting properties (preset, tune, lookahead)
    are deliberately left at webrtcsink's defaults so this stays a quality
    change, not a latency change.
    """
    factory = encoder.get_factory()
    name    = factory.get_name() if factory else '<unknown>'
    if name in ('x264enc', 'nvh264enc'):
        # H.264 QP scale is 0–51.
        _set_if_present(encoder, 'qp-min', 0)
        _set_if_present(encoder, 'qp-max', 51)
    elif name in ('vp8enc', 'vp9enc'):
        # libvpx uses 0–63 internally; gstreamer exposes that range.
        _set_if_present(encoder, 'min-quantizer', 0)
        _set_if_present(encoder, 'max-quantizer', 63)
    elif name in ('av1enc', 'rav1enc', 'svtav1enc', 'aomenc'):
        # AV1 QP scale per the spec is 0–255; encoder bindings vary, so we
        # just probe both common spellings.
        _set_if_present(encoder, 'min-quantizer', 0)
        _set_if_present(encoder, 'max-quantizer', 255)
        _set_if_present(encoder, 'min-qp', 0)
        _set_if_present(encoder, 'max-qp', 255)
    print(f'[service] encoder-setup: tuned {name} for lossless QP floor',
          flush=True)
    # Returning False tells webrtcsink we did not replace the encoder, only
    # adjusted its properties — webrtcsink keeps managing rate control.
    return False


# Debounce state for the q_arch overrun warning — fires at most once per 30s
# of overrun activity so a sustained encoder lag doesn't flood the log.
_Q_ARCH_OVERRUN_LOG_INTERVAL_SEC = 30
_q_arch_overrun_state = {'last_warn_ts': 0.0}


def _on_q_arch_overrun(_queue):
    """Log a debounced warning when q_arch evicts a frame.

    The queue runs with leaky=downstream, so when it fills the oldest
    buffer is dropped and the tee push returns immediately — the WebRTC
    branch stays smooth.  We deliberately don't block: a `tee` element's
    chain function is synchronous, so blocking on q_arch would also
    block the push to q_webrtc and stall the live viewer.

    The cost is that a sustained archive encoder lag produces visible
    jumps in the recording.  This handler surfaces that to the operator
    so they can act (lower ARCHIVE_QP, switch ARCHIVE_QUALITY=legacy,
    raise ARCHIVE_QUEUE_MAX_BYTES, or add hardware encoding).
    """
    now = time.monotonic()
    if now - _q_arch_overrun_state['last_warn_ts'] < _Q_ARCH_OVERRUN_LOG_INTERVAL_SEC:
        return
    _q_arch_overrun_state['last_warn_ts'] = now
    print('[service] WARNING: q_arch overflowed — archive encoder is falling '
          'behind; dropping oldest queued frames to keep WebRTC smooth. '
          'Consider lowering ARCHIVE_QP, switching ARCHIVE_QUALITY=legacy, '
          'raising ARCHIVE_QUEUE_MAX_BYTES, or adding hardware encoding.',
          file=sys.stderr, flush=True)


def build_archive_encoder():
    """Pick the first available encoder factory and apply its properties."""
    plan = archive_encoder_plan(
        quality=ARCHIVE_QUALITY, qp=ARCHIVE_QP,
        bitrate_cap=ARCHIVE_BITRATE_CAP, bitrate_legacy=ARCHIVE_BITRATE,
    )
    for factory_name, props in plan:
        if not Gst.ElementFactory.find(factory_name):
            continue
        enc = Gst.ElementFactory.make(factory_name, 'arch_enc')
        for prop_name, value in props.items():
            _set_if_present(enc, prop_name, value)
        if ARCHIVE_QUALITY == 'legacy':
            print(f'[service] archive encoder: {factory_name} '
                  f'mode=legacy bitrate={ARCHIVE_BITRATE}kbps', flush=True)
        elif ARCHIVE_QUALITY == 'lossless':
            print(f'[service] archive encoder: {factory_name} '
                  f'mode=lossless (QP=0, max={ARCHIVE_BITRATE_CAP}kbps)',
                  flush=True)
        else:
            print(f'[service] archive encoder: {factory_name} '
                  f'mode=visually-lossless (QP={ARCHIVE_QP}, '
                  f'max={ARCHIVE_BITRATE_CAP}kbps)', flush=True)
        return enc
    print('[service] ERROR: no archive encoder available '
          '(need nvh264enc or x264enc)', file=sys.stderr)
    sys.exit(1)


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
    os.makedirs(ARCHIVE_LIVE_DIR, exist_ok=True)

    segment_ns      = ARCHIVE_SEGMENT_SEC * Gst.SECOND
    archive_pattern = os.path.join(ARCHIVE_LIVE_DIR, f'{archive_prefix}-%05d.mkv')

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
    print(f'  Live segments     : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Completed archive : {ARCHIVE_DIR}')
    print(f'  Archive quality   : {ARCHIVE_QUALITY}'
          + (f' (QP={ARCHIVE_QP})' if ARCHIVE_QUALITY == 'visually-lossless'
             else f' (legacy bitrate={ARCHIVE_BITRATE}kbps)' if ARCHIVE_QUALITY == 'legacy'
             else ''))
    print(f'  Archive q cap     : {ARCHIVE_QUEUE_MAX_BYTES} bytes / '
          f'{ARCHIVE_QUEUE_MAX_SEC}s (leaky=downstream)')
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
    # Bound q_arch and leak the oldest buffer when full.  See the comment on
    # _on_q_arch_overrun below for the rationale; in short, leaky=downstream
    # keeps the WebRTC branch isolated from a slow archive encoder at the
    # cost of dropping archive frames during sustained lag.
    q_arch.set_property('max-size-buffers', 0)        # gate via bytes/time only
    q_arch.set_property('max-size-bytes',   ARCHIVE_QUEUE_MAX_BYTES)
    q_arch.set_property('max-size-time',    ARCHIVE_QUEUE_MAX_SEC * Gst.SECOND)
    q_arch.set_property('leaky', 2)                   # 2 = downstream, drop oldest
    q_arch.connect('overrun', _on_q_arch_overrun)
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

    # ── Finalize completed segments: remux MKV → MP4 (faststart), then move
    # GStreamer's internal clock and Python's time.time() may use different
    # reference points (host CLOCK_MONOTONIC vs container-scoped monotonic).
    # Avoid the mismatch entirely by stamping segments with time.time() at
    # callback invocation.  The end of one fragment == start of the next
    # because both reads happen in the same callback call.
    #
    # The remux runs on a single background daemon thread so the GStreamer
    # streaming thread (the one that fires format-location-full) never blocks
    # on ffmpeg I/O.  ffmpeg is invoked with `-c copy` so no re-encoding
    # happens; `+faststart` rewrites the moov atom to the front of the file
    # for web-player progressive playback.
    _fragment_starts = {}    # fragment_id -> start nanoseconds (Unix wall-clock)
    _finalize_queue  = queue.Queue()

    def _remux_to_mp4(src, dst):
        """Remux MKV → MP4 with faststart.  Returns True on success.

        `-f mp4` is passed explicitly because the on-disk filename has a
        `.mp4.part` suffix during the write — ffmpeg's normal
        extension-based muxer detection sees `.part` and fails with
        "Unable to choose an output format".  The atomic rename to the
        final `.mp4` happens after this call returns.
        """
        result = subprocess.run(
            ['ffmpeg', '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
             '-fflags', '+genpts', '-i', src,
             '-c', 'copy', '-map', '0:v:0',
             '-movflags', '+faststart',
             '-f', 'mp4', dst],
            capture_output=True,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode('utf-8', errors='replace')[-1000:]
            print(f'[service] ERROR: ffmpeg remux {src} -> {dst} failed '
                  f'(exit {result.returncode}): {stderr}',
                  file=sys.stderr, flush=True)
            return False
        return True

    def _finalize_fragment(src, dst):
        """Remux src .mkv to dst .mp4 (faststart) and remove the source.

        ffmpeg writes the new MP4 alongside the source in ARCHIVE_LIVE_DIR
        (under a `.part` suffix) so /archive never sees a partial file.
        `+faststart` does a 2-pass write — the file passes through several
        intermediate states before the moov atom lands at the front — and
        keeping all of that confined to ARCHIVE_LIVE_DIR means /archive's
        `*.mp4` glob only ever lists fully-finalized segments.

        Once ffmpeg succeeds we publish to ARCHIVE_DIR via a `.part` →
        final-name rename so the publication itself is atomic within
        ARCHIVE_DIR even when ARCHIVE_LIVE_DIR and ARCHIVE_DIR are on
        different filesystems (the cross-fs copy lands under .part; the
        rename to the final name is on a single filesystem).

        Falls back to moving the .mkv (under a .mkv name in ARCHIVE_DIR)
        when ffmpeg fails so we never lose the recording.
        """
        tmp_dst        = os.path.join(ARCHIVE_LIVE_DIR,
                                      os.path.basename(dst) + '.part')
        publish_part   = dst + '.part'

        if _remux_to_mp4(src, tmp_dst):
            try:
                shutil.move(tmp_dst, publish_part)
                os.rename(publish_part, dst)
            except OSError as exc:
                print(f'[service] WARNING: could not publish {tmp_dst} as '
                      f'{dst}: {exc}', file=sys.stderr, flush=True)
                for path in (tmp_dst, publish_part):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
                return
            try:
                os.unlink(src)
            except OSError as exc:
                print(f'[service] WARNING: could not unlink {src}: {exc}',
                      file=sys.stderr, flush=True)
            print(f'[service] archive: {os.path.basename(src)}'
                  f' -> {os.path.basename(dst)}', flush=True)
            return

        # ffmpeg failed — clean up its partial output and keep the .mkv.
        try:
            os.unlink(tmp_dst)
        except FileNotFoundError:
            pass

        fallback      = os.path.splitext(dst)[0] + '.mkv'
        fallback_part = fallback + '.part'
        try:
            shutil.move(src, fallback_part)
            os.rename(fallback_part, fallback)
            print(f'[service] archive: fallback move {os.path.basename(src)}'
                  f' -> {os.path.basename(fallback)}', flush=True)
        except OSError as exc:
            print(f'[service] WARNING: could not move {src} to {fallback}: {exc}',
                  file=sys.stderr, flush=True)
            try:
                os.unlink(fallback_part)
            except FileNotFoundError:
                pass

    def _finalize_worker():
        while True:
            item = _finalize_queue.get()
            try:
                if item is None:
                    return
                src, dst = item
                _finalize_fragment(src, dst)
            finally:
                _finalize_queue.task_done()

    threading.Thread(target=_finalize_worker, name='archive-finalize',
                     daemon=True).start()

    def _enqueue_fragment(frag_id, end_ns):
        if frag_id not in _fragment_starts:
            return
        start_ns = _fragment_starts.pop(frag_id)
        src = os.path.join(ARCHIVE_LIVE_DIR, f'{archive_prefix}-{frag_id:05d}.mkv')
        dst = renamed_segment_path(src, start_ns, end_ns, archive_prefix,
                                   dest_dir=ARCHIVE_DIR, ext='.mp4')
        _finalize_queue.put((src, dst))

    def _on_format_location_full(_splitmux, fragment_id, first_sample):
        now_ns = time.time_ns()
        _enqueue_fragment(fragment_id - 1, now_ns)
        _fragment_starts[fragment_id] = now_ns
        return None

    archive.connect('format-location-full', _on_format_location_full)
    print('[service] archive: using format-location-full (PTS-based timestamps)',
          flush=True)

    # ── Configure videocrop per screen: each screen's crop trims are
    # pre-computed by desktop_config so they remain correct in caster mode
    # even when the source's actual dimensions differ from the configured
    # ones (e.g. CROP_HEIGHT-based splits use trims expressed directly in
    # source coordinates).
    for s, _q, vc, _wsk in screen_branches:
        vc.set_property('left',   s['cropLeft'])
        vc.set_property('top',    s['cropTop'])
        vc.set_property('right',  s['cropRight'])
        vc.set_property('bottom', s['cropBottom'])

    # ── Configure browser webrtcsink instances
    #
    # The min/start/max bitrate triple expands the window REMB is allowed to
    # drive the per-peer encoder between.  webrtcsink defaults cap the ceiling
    # near 8 Mbps which is well below visually-lossless 1080p30 territory; we
    # raise it so the encoder can actually reach the lossless / visually-
    # lossless tiers when the network has the headroom.  The encoder will not
    # *spend* the ceiling on static content — it'll just sit at low QP and low
    # bitrate, and burst into the headroom when motion appears.
    sinks = [(ws_full, full_sig_port)]
    sinks.extend((wsk, s['signallingPort']) for s, _q, _vc, wsk in screen_branches)
    for sink, port in sinks:
        sink.get_property('signaller').set_property('uri', f'ws://127.0.0.1:{port}')
        sink.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))
        sink.set_property('min-bitrate',   WEBRTC_MIN_BITRATE)
        sink.set_property('start-bitrate', WEBRTC_START_BITRATE)
        sink.set_property('max-bitrate',   WEBRTC_MAX_BITRATE)
        sink.connect('encoder-setup', _on_encoder_setup)
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
            # Finalize the last fragment — format-location-full won't fire for it.
            if _fragment_starts:
                end_ns = time.time_ns()
                _enqueue_fragment(max(_fragment_starts), end_ns)
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
        # Drain pending finalizations so the last fragment lands in /archive
        # before we exit.
        _finalize_queue.join()
        print('[service] Pipeline stopped')


if __name__ == '__main__':
    main()
