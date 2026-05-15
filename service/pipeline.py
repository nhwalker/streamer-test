#!/usr/bin/env python3
"""
pipeline.py -- desktop-stream-service: ingress -> tee -> archive + WebRTC.

The pipeline runs on the GPU when the gst-cuda elements are registered
(which happens automatically when libcuda.so is dlopen-able — typically
because nvidia-container-toolkit has injected the driver libraries) and
falls back to the equivalent software elements otherwise.

GPU path (preferred):
  ximagesrc -> videorate -> cudaupload -> cudaconvertscale -> tee
                                          [video/x-raw(memory:CUDAMemory),
                                           format=NV12,width=W,height=H]

CPU fallback path:
  ximagesrc -> videorate -> videoscale  -> videoconvert      -> tee

A single `cudaupload` at the head of the GPU path keeps every downstream
element working in CUDA memory; `tee` distributes GPU-buffer references
rather than re-copying the frame for each branch.  The archive encoder
(`nvcudah264enc` when available, else `nvh264enc`) and per-tier
`cudascale` (WebRTC fan-out) consume the CUDA buffers directly without
re-uploading.  `nvcudah264enc` is preferred because it shares the
gst-cuda buffer pool with upstream `cudaupload`/`cudaconvertscale`,
skipping the internal context-rebind that `nvh264enc` performs.

The tee fans out:
  tee. -> [cudadownload?] -> encoder -> h264parse -> splitmuxsink   (archive)
  tee. -> tee_webrtc

The optional `cudadownload` between the archive queue and the encoder is
only inserted when the runtime falls back to `x264enc` (software).
Both NVENC variants accept `video/x-raw(memory:CUDAMemory)` natively,
so on the hardware path the archive branch has zero copies between tee
and encoder.

Archive segments are written as fragmented MP4 directly in ARCHIVE_LIVE_DIR
(mp4mux with fragment-duration + streamable, so the in-progress fragment
is readable mid-write), then renamed/moved into ARCHIVE_DIR when the
fragment rotates.  Each segment file is already a valid faststart-style
fmp4 (`ftyp + moov + (moof+mdat)*`) the moment splitmuxsink closes it —
no remux is needed, so rollover is just a rename on same-fs setups or a
single byte-for-byte copy when ARCHIVE_LIVE_DIR and ARCHIVE_DIR are on
different filesystems.  The move runs on a single background worker so
the GStreamer streaming thread is never blocked even on slow bulk
storage.

The WebRTC branch is a *ladder* of webrtcsinks per stream — one tier per
entry in WEBRTC_SCALE_LADDER.  Each browser picks the smallest tier whose
dimensions still match its rendered video element and connects to that
tier's signalling port; webrtcsink only builds its per-consumer encoder
when a consumer actually subscribes, so an idle tier costs zero encoder
CPU.  Per-tier scaling stays on the GPU (`cudascale`) when available; on
the CPU fallback it uses `videoscale` and is a few ms/frame per tier.

GPU path:
            tee_webrtc. -> queue -> cudascale -> capsfilter(WxH,CUDA) -> webrtcsink (full,  tier i)
            tee_webrtc. -> queue -> cudadownload -> videocrop -> cudaupload
                                    -> cudascale -> capsfilter(WxH,CUDA) -> webrtcsink (screen, tier i)

CPU fallback:
            tee_webrtc. -> queue -> videoscale -> capsfilter(WxH) -> webrtcsink (full,  tier i)
            tee_webrtc. -> queue -> videocrop -> videoscale -> capsfilter(WxH) -> webrtcsink (screen, tier i)

The screen branches round-trip through system memory because gst-cuda
ships no native crop element in this build; the cost is bounded —
`videocrop` runs on CPU but the upload-after-crop covers only the cropped
region, smaller than the source frame.

The resolution, the list of per-screen regions, and the per-tier widths,
heights and signalling ports all come from desktop_config.load_config();
see desktop_config.py for the env-var contract.

Environment variables:
  DISPLAY                X11 display                              (:0)
  STREAM_FRAMERATE       frames per second                        (30)

  ARCHIVE_DIR            output dir for completed .mp4 segments   (/archive)
                         (fragmented MP4, web-player friendly)
  ARCHIVE_LIVE_DIR       output dir for the in-progress .mp4      (/archive-live)
                         segment; written as fragmented MP4 by
                         mp4mux so it is readable mid-write, then
                         renamed/moved to ARCHIVE_DIR on rotation
                         (no re-encoding, no remux step)
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
DESKTOP_SPLITS, and SIGNALLING_PORT.
"""
import os
import queue
import shutil
import signal
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

DISPLAY               = os.environ.get('DISPLAY', ':0')
FRAMERATE             = os.environ.get('STREAM_FRAMERATE', '30')

ARCHIVE_DIR             = os.environ.get('ARCHIVE_DIR', '/archive')
ARCHIVE_LIVE_DIR        = os.environ.get('ARCHIVE_LIVE_DIR', '/archive-live')
ARCHIVE_SEGMENT_SEC     = int(os.environ.get('ARCHIVE_SEGMENT_SEC', '600'))
ARCHIVE_QUALITY         = os.environ.get('ARCHIVE_QUALITY', 'visually-lossless').strip().lower()
ARCHIVE_QP              = int(os.environ.get('ARCHIVE_QP', '18'))
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


# Elements required to take the GPU pipeline path.  They all live in
# libgstnvcodec.so, which dlopen's libcuda.so when the plugin loads; with
# the NVIDIA container toolkit injecting the driver libraries they light up
# as a set (no separate CUDA-runtime install needed).  When the probe fails
# the pipeline falls back to the equivalent software elements without any
# behavioural change for callers.
_CUDA_ELEMENTS = ('cudaupload', 'cudadownload', 'cudaconvertscale', 'cudascale')


def _have_cuda():
    """Return True when every gst-cuda element we rely on is registered."""
    return all(Gst.ElementFactory.find(n) is not None for n in _CUDA_ELEMENTS)


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
    if name in ('x264enc', 'nvh264enc', 'nvcudah264enc'):
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
        quality=ARCHIVE_QUALITY, qp=ARCHIVE_QP, bitrate_legacy=ARCHIVE_BITRATE,
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
                  f'mode=lossless (QP=0)', flush=True)
        else:
            print(f'[service] archive encoder: {factory_name} '
                  f'mode=visually-lossless (QP={ARCHIVE_QP})', flush=True)
        return enc
    print('[service] ERROR: no archive encoder available '
          '(need nvh264enc/nvcudah264enc or x264enc)', file=sys.stderr)
    sys.exit(1)


def main():
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
    archive_pattern = os.path.join(ARCHIVE_LIVE_DIR, f'{archive_prefix}-%05d.mp4')

    print('[service] Starting stream service:', flush=True)
    print(f'  Desktop name      : {desktop_name}')
    print(f'  Display           : {DISPLAY}')
    print(f'  Resolution        : {width}x{height} @ {FRAMERATE} fps')
    print(f'  Live segments     : {archive_pattern} ({ARCHIVE_SEGMENT_SEC}s segments)')
    print(f'  Completed archive : {ARCHIVE_DIR}')
    print(f'  Archive quality   : {ARCHIVE_QUALITY}'
          + (f' (QP={ARCHIVE_QP})' if ARCHIVE_QUALITY == 'visually-lossless'
             else f' (legacy bitrate={ARCHIVE_BITRATE}kbps)' if ARCHIVE_QUALITY == 'legacy'
             else ''))
    print(f'  Archive q cap     : {ARCHIVE_QUEUE_MAX_BYTES} bytes / '
          f'{ARCHIVE_QUEUE_MAX_SEC}s (leaky=downstream)')
    full_tiers_cfg = config.get('fullTiers', [])
    if full_tiers_cfg:
        print('  Signalling /      :')
        for t in full_tiers_cfg:
            print(f'    tier scale={t["scale"]:<5} ws://127.0.0.1:{t["signallingPort"]}'
                  f'  {t["width"]}x{t["height"]}')
    else:
        print(f'  Signalling /      : ws://127.0.0.1:{full_sig_port}')
    for s in screens:
        path = s['path']
        if s.get('tiers'):
            print(f'  Signalling {path:<7s}: '
                  f'(region {s["width"]}x{s["height"]}+{s["x"]}+{s["y"]})')
            for t in s['tiers']:
                print(f'    tier scale={t["scale"]:<5} ws://127.0.0.1:{t["signallingPort"]}'
                      f'  {t["width"]}x{t["height"]}')
        else:
            print(f'  Signalling {path:<7s}: ws://127.0.0.1:{s["signallingPort"]}'
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

    # Probe gst-cuda once so every branch below picks the GPU or CPU element
    # set consistently.  Logged so operators can see at a glance which path
    # the pipeline chose at startup.
    have_cuda = _have_cuda()
    print(f'  Pipeline mode     : '
          f'{"GPU (cuda*)" if have_cuda else "CPU (videoscale/videoconvert)"}',
          flush=True)

    # ── Ingress + tee (the convert/scale elements are created below where
    #    we know whether to use the GPU or CPU variants).
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

    # ── Browser WebRTC branch: fan out to full / per-screen streams ×
    # per-tier sub-streams.  Each (stream, tier) pair gets its own:
    #
    #   GPU path:
    #     queue → [cudadownload → videocrop → cudaupload] → cudascale
    #           → capsfilter(W,H,CUDAMemory) → webrtcsink
    #
    #   CPU fallback:
    #     queue → [videocrop] → videoscale → capsfilter(W,H) → webrtcsink
    #
    # The crop is only present for screen sub-streams (the full stream
    # serves the whole frame).  On the GPU path the crop runs in system
    # memory because gst-cuda has no native crop element here, so screen
    # branches round-trip via cudadownload → videocrop → cudaupload; the
    # cudaupload only carries the cropped (smaller) result.  Each tier
    # duplicates the crop rather than sharing one crop with a downstream
    # tee because each tier scales to different dimensions; sharing the
    # crop across tiers per screen is a possible future optimisation.
    #
    # webrtcsink already lazily creates its per-consumer encoder, so tiers
    # with no consumer pay zero encoder CPU.  The upstream scale runs
    # continuously regardless — `cudascale` is essentially free on the GPU
    # path; `videoscale` is a few ms/frame per tier on the CPU fallback.
    # A future optimisation can gate the scale with a valve driven by
    # webrtcsink's consumer-added / consumer-removed signals; we leave
    # that out for now because in gst-plugins-rs 0.13.3 those hooks
    # didn't reliably keep the pipeline in PLAYING.
    q_webrtc   = make('queue',       'q_webrtc')
    tee_webrtc = make('tee',         't_webrtc')

    full_tiers = config.get('fullTiers') or [{
        # Legacy callers without tier info get a single passthrough tier.
        'scale': 1.0, 'width': width, 'height': height,
        'signallingPort': full_sig_port,
    }]

    # Flat list of all webrtcsink-bearing branches.  Each entry is a dict
    # so the downstream config/link loops can pull whichever pieces they
    # need without juggling positional tuples.
    webrtc_branches = []

    def _build_tier_branch(stream_label, tier_idx, tier, screen_cfg=None):
        suffix = f'{stream_label}_t{tier_idx}'
        q     = make('queue',       f'q_{suffix}')
        # Elements between the per-branch queue and the scale.  On the CPU
        # path this is just `videocrop` for screen branches; on the GPU path
        # screen branches round-trip through system memory because gst-cuda
        # has no native crop element — cudadownload → videocrop → cudaupload.
        pre = []
        if screen_cfg is not None:
            if have_cuda:
                pre.append(make('cudadownload', f'cudadl_{suffix}'))
            vc = make('videocrop',  f'crop_{suffix}')
            vc.set_property('left',   screen_cfg['cropLeft'])
            vc.set_property('top',    screen_cfg['cropTop'])
            vc.set_property('right',  screen_cfg['cropRight'])
            vc.set_property('bottom', screen_cfg['cropBottom'])
            pre.append(vc)
            if have_cuda:
                pre.append(make('cudaupload',   f'cudaup_{suffix}'))
        if have_cuda:
            sc    = make('cudascale',  f'scale_{suffix}')
            caps_str = (f'video/x-raw(memory:CUDAMemory),format=NV12,'
                        f'width={tier["width"]},height={tier["height"]}')
        else:
            sc    = make('videoscale', f'scale_{suffix}')
            caps_str = (f'video/x-raw,'
                        f'width={tier["width"]},height={tier["height"]}')
        capsf = make('capsfilter',  f'capsf_{suffix}')
        capsf.set_property('caps', Gst.Caps.from_string(caps_str))
        wsk   = make('webrtcsink',  f'ws_{suffix}')
        webrtc_branches.append({
            'label': suffix,
            'queue': q, 'pre': pre,
            'scale': sc, 'capsf': capsf, 'sink': wsk,
            'port':  tier['signallingPort'],
            'tier':  tier,
            'screen_path': screen_cfg['path'] if screen_cfg else '/',
        })

    for tier_idx, tier in enumerate(full_tiers):
        _build_tier_branch('full', tier_idx, tier, screen_cfg=None)
    for s in screens:
        # desktop_config emits per-screen tiers sized to the cropped
        # region; we trust that list end-to-end.
        for tier_idx, tier in enumerate(s.get('tiers') or [{
                'scale': 1.0, 'width': s['width'], 'height': s['height'],
                'signallingPort': s['signallingPort']}]):
            _build_tier_branch(s['name'], tier_idx, tier, screen_cfg=s)

    # ── Configure ingress source
    # GPU path: upload once into CUDA memory, then run convert+scale on the
    #   GPU in a single cudaconvertscale op.  Every downstream branch (tee →
    #   nvh264enc, tee → tee_webrtc → cudascale → ...) stays in CUDA memory.
    # CPU fallback: same shape as the legacy pipeline — videoscale + videoconvert.
    xsrc      = make('ximagesrc',   'xsrc')
    vrate     = make('videorate',   'vrate')
    xsrc.set_property('display-name', DISPLAY)
    xsrc.set_property('use-damage', False)
    xsrc.link_filtered(
        vrate,
        Gst.Caps.from_string(f'video/x-raw,framerate={FRAMERATE}/1'),
    )
    if have_cuda:
        cudaup = make('cudaupload',       'cudaup_src')
        cudacs = make('cudaconvertscale', 'cudacs_src')
        vrate.link(cudaup)
        cudaup.link(cudacs)
        cudacs.link_filtered(
            tee,
            Gst.Caps.from_string(
                f'video/x-raw(memory:CUDAMemory),format=NV12,'
                f'width={width},height={height}'
            ),
        )
    else:
        vscale   = make('videoscale',   'vscale')
        vconvert = make('videoconvert', 'vconvert')
        vrate.link(vscale)
        vscale.link_filtered(
            vconvert,
            Gst.Caps.from_string(f'video/x-raw,width={width},height={height}'),
        )
        vconvert.link(tee)

    # ── Configure archive
    # config-interval=-1: SPS/PPS before every keyframe → each segment is
    # independently decodable without seeking to the start.
    #
    # splitmuxsink + mp4mux with fragment-duration produces a fragmented MP4
    # per segment.  Completed files (after splitmuxsink EOSes the muxer on
    # rotation) are fully valid MP4 — moov is written at the end by mp4mux
    # and ffmpeg/browsers parse them fine.  The active in-progress file
    # has no parseable moov; /archive's active-segment branch handles that
    # case by walking the file's `mdat` boxes and remuxing on demand (see
    # `_copy_active_to_stage` in web_server.py).
    #
    # We tried fragment-mode=first-moov here, hoping to get moov-at-front
    # mid-write — but in this build of mp4mux it didn't deliver a
    # parseable in-progress file in practice (the integration tests still
    # saw "moov atom not found").  The simpler hybrid wins: rollover is a
    # rename (no ffmpeg), and only the active-segment serve path pays the
    # remux cost.
    #
    # fragment-duration=1000 ms aligns each fragment with our 30-frame GOP
    # (1 s at 30 fps), so each mdat box ends up holding one GOP — a
    # convenient unit for the mdat-walker in web_server.py.
    arch_h264.set_property('config-interval', -1)
    archive.set_property('muxer-factory', 'mp4mux')
    muxer_props = Gst.Structure.new_from_string(
        'properties, fragment-duration=(uint)1000'
    )
    archive.set_property('muxer-properties', muxer_props)
    archive.set_property('location', archive_pattern)
    archive.set_property('max-size-time', segment_ns)

    # ── Finalize completed segments: rename live .mp4 → timestamped .mp4
    # GStreamer's internal clock and Python's time.time() may use different
    # reference points (host CLOCK_MONOTONIC vs container-scoped monotonic).
    # Avoid the mismatch entirely by stamping segments with time.time() at
    # callback invocation.  The end of one fragment == start of the next
    # because both reads happen in the same callback call.
    #
    # Because the live container is fragmented MP4, the file splitmuxsink
    # just closed is already a complete, web-player-friendly MP4 — no
    # remux step is needed.  Finalize is therefore just "rename to the
    # timestamp-based name and move to ARCHIVE_DIR".  On same-fs setups
    # that's an instant os.rename; on cross-fs setups (e.g. LIVE on tmpfs,
    # ARCHIVE on bulk disk) shutil.move performs a single read+write copy.
    # The worker thread keeps that copy off the GStreamer streaming thread
    # so cross-fs publication can never stall the pipeline.
    _fragment_starts = {}    # fragment_id -> start nanoseconds (Unix wall-clock)
    _finalize_queue  = queue.Queue()

    def _finalize_fragment(src, dst):
        """Publish a completed live .mp4 fragment as a timestamped .mp4.

        The move is staged via a `.part` suffix in ARCHIVE_DIR so /archive's
        `*.mp4` glob never matches a partially-copied file: on cross-fs
        setups shutil.move writes bytes into the `.part` name, then the
        atomic os.rename publishes them under the final name in one step.
        On same-fs setups shutil.move is itself an os.rename, so this is
        effectively a no-op extra rename — still cheap.

        If the move ever fails (disk full, permission, etc.) we leave the
        source in ARCHIVE_LIVE_DIR under its sequential name so the
        recording is not lost; the operator can recover manually.
        """
        publish_part = dst + '.part'
        try:
            shutil.move(src, publish_part)
            os.rename(publish_part, dst)
        except OSError as exc:
            print(f'[service] WARNING: could not publish {src} as {dst}: {exc}',
                  file=sys.stderr, flush=True)
            try:
                os.unlink(publish_part)
            except FileNotFoundError:
                pass
            return
        print(f'[service] archive: {os.path.basename(src)}'
              f' -> {os.path.basename(dst)}', flush=True)

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
        src = os.path.join(ARCHIVE_LIVE_DIR, f'{archive_prefix}-{frag_id:05d}.mp4')
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

    # ── Configure browser webrtcsink instances
    #
    # The min/start/max bitrate triple expands the window REMB is allowed to
    # drive the per-peer encoder between.  webrtcsink defaults cap the ceiling
    # near 8 Mbps which is well below visually-lossless 1080p30 territory; we
    # raise it so the encoder can actually reach the lossless / visually-
    # lossless tiers when the network has the headroom.  The encoder will not
    # *spend* the ceiling on static content — it'll just sit at low QP and low
    # bitrate, and burst into the headroom when motion appears.
    for branch in webrtc_branches:
        sink = branch['sink']
        port = branch['port']
        sink.get_property('signaller').set_property('uri', f'ws://127.0.0.1:{port}')
        sink.set_property('video-caps', Gst.Caps.from_string(WEBRTC_VIDEO_CAPS))
        sink.set_property('min-bitrate',   WEBRTC_MIN_BITRATE)
        sink.set_property('start-bitrate', WEBRTC_START_BITRATE)
        sink.set_property('max-bitrate',   WEBRTC_MAX_BITRATE)
        sink.connect('encoder-setup', _on_encoder_setup)
        if STUN:
            sink.set_property('stun-server', STUN)

    # ── Static links: ingress already linked to `tee` above; here we wire
    #    the tee outputs to the archive and webrtc branches.

    tee.link(q_arch)
    # On the GPU path, nvcudah264enc / nvh264enc consume CUDA memory natively
    # — no extra copy needed.  If the runtime fell back to x264enc (CPU
    # encoder), we insert a single cudadownload so the encoder sees system
    # memory.
    arch_factory = arch_enc.get_factory()
    arch_is_software = (
        arch_factory is not None and arch_factory.get_name() == 'x264enc'
    )
    if have_cuda and arch_is_software:
        cudadl_arch = make('cudadownload', 'cudadl_arch')
        q_arch.link(cudadl_arch)
        cudadl_arch.link(arch_enc)
    else:
        q_arch.link(arch_enc)
    arch_enc.link(arch_h264)
    # Explicitly negotiate AVCC (length-prefixed) format so mp4mux writes
    # SPS/PPS in the `avcC` box and NALUs as length-prefixed — mp4mux only
    # accepts AVCC, and pinning the caps here means h264parse handles any
    # byte-stream → AVCC conversion before the muxer sees the buffers.
    arch_h264.link_filtered(
        archive,
        Gst.Caps.from_string('video/x-h264, stream-format=avc, alignment=au'),
    )

    tee.link(q_webrtc)
    q_webrtc.link(tee_webrtc)

    for branch in webrtc_branches:
        tee_webrtc.link(branch['queue'])
        prev = branch['queue']
        for el in branch['pre']:
            prev.link(el)
            prev = el
        prev.link(branch['scale'])
        branch['scale'].link(branch['capsf'])
        branch['capsf'].link(branch['sink'])

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
