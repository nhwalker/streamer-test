"""
stream_command.py -- pure builders for the ffmpeg capture/encode process
and the MediaMTX server configuration.

Everything in this module is side-effect free (no subprocess, no
filesystem, no X server) so unit tests can exercise the exact argv /
config text the runtime will use.  pipeline.py owns process spawning and
supervision; entrypoint.sh writes the MediaMTX config at container start.

Topology (single ffmpeg process — one X11 capture, fan-out via
filter_complex):

    [0:v] x11grab
      -> scale=WxH,format=nv12            (one convert, like the old
                                           cudaconvertscale -> tee)
      -> split
           -> full   tier 0..N  -> h264 -> RTSP rtsp://127.0.0.1:8554/full_t<N>
           -> screen tier 0..N  -> crop -> scale -> h264 -> RTSP .../<name>_t<N>
           -> archive           -> h264 -> fragmented-MP4 segments

MediaMTX repackages each RTSP path as WebRTC and serves browsers via
WHEP at  http://<host>:<WEBRTC_PORT>/<path>/whep .

Environment variables consumed here (all optional):

  LIVE_CQ         Constant-quality target for the live encoders (H.264 QP
                  scale, lower = better).  18 is the conventional
                  visually-lossless threshold.                       (18)
  LIVE_MAXRATE    Hard bitrate cap for the full-resolution tier, e.g.
                  '8M' or '6000k'.  Smaller tiers are capped
                  proportionally to their pixel count.  Set this to the
                  worst-case provisioned per-viewer bandwidth.       (8M)
  LIVE_BUFSIZE    VBV buffer for the full-resolution tier.  Smaller =
                  smoother bitrate, more clarity sacrificed on motion
                  bursts; larger = more motion detail, bigger bursts.
                  Scaled per tier like LIVE_MAXRATE.        (2x LIVE_MAXRATE)
  LIVE_GOP        Keyframe interval in frames.  Defaults to one second
                  (== STREAM_FRAMERATE).  Keep this short: MediaMTX
                  cannot request keyframes from an RTSP publisher, so a
                  viewer joining mid-stream sees nothing until the next
                  keyframe — the GOP length IS the worst-case join time.
  MEDIAMTX_RTSP_PORT       localhost RTSP ingest port            (8554)
  WEBRTC_PORT              MediaMTX WHEP/HTTP port               (8889)
  WEBRTC_UDP_PORT          MediaMTX ICE/UDP media port           (8189)
  WEBRTC_ADDITIONAL_HOSTS  comma-separated extra IPs/hostnames to
                           advertise in ICE candidates (for viewers that
                           cannot reach the interface IPs)         ("")
"""

# ── Rate-control choice (decision record) ────────────────────────────────────
# The live encoders run capped VBR at constant quality:
#
#     -rc vbr -cq <LIVE_CQ> -b:v 0 -maxrate <cap> -bufsize <2x cap>
#
# On static desktop content this sits at a low bitrate with QP ~= LIVE_CQ
# (visually lossless at the default 18); motion bursts are absorbed by
# rising QP under the cap, i.e. a transient clarity dip, never a bitrate
# spike beyond -maxrate.  This is the closest fixed-parameter match to the
# old per-viewer REMB behaviour (which ramped each viewer's encoder toward
# visually-lossless whenever bandwidth allowed).
#
# ALTERNATIVE — fixed CBR:
#
#     -rc cbr -b:v <rate> -maxrate <rate> -bufsize <rate>
#
# Choose CBR when the provisioned link must see a *constant* bitrate
# (e.g. strict traffic-shaping or capacity accounting on the path).  The
# cost is quality: static content is padded/held at the target rate's
# quality level instead of dropping to near-zero bitrate at high quality,
# and clarity under motion is generally lower than capped-VBR at the same
# cap.  To switch, replace the rate-control args in live_encoder_args()
# below; no other part of the pipeline cares.
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_LIVE_CQ      = 18
DEFAULT_LIVE_MAXRATE = '8M'
DEFAULT_RTSP_PORT    = 8554
DEFAULT_WEBRTC_PORT  = 8889
DEFAULT_WEBRTC_UDP   = 8189

# Never cap a tier below this, no matter how small its pixel count —
# H.264 needs a floor to stay decodable at all.
_MIN_TIER_RATE = 500_000

_RATE_SUFFIXES = {'k': 1_000, 'K': 1_000, 'm': 1_000_000, 'M': 1_000_000}


def parse_rate(value):
    """Parse an ffmpeg-style bitrate string ('8M', '6000k', '500000') to bps."""
    s = str(value).strip()
    if not s:
        raise ValueError('empty bitrate')
    if s[-1] in _RATE_SUFFIXES:
        return int(float(s[:-1]) * _RATE_SUFFIXES[s[-1]])
    return int(float(s))


def _tier_rates(env, tier_w, tier_h, full_w, full_h):
    """Return (maxrate_bps, bufsize_bps) for a tier, scaled by pixel count.

    LIVE_MAXRATE / LIVE_BUFSIZE describe the full-resolution tier; smaller
    tiers get proportionally smaller caps so the ladder's total bandwidth
    stays predictable.
    """
    base_rate = parse_rate(env.get('LIVE_MAXRATE') or DEFAULT_LIVE_MAXRATE)
    ratio = (tier_w * tier_h) / float(full_w * full_h)
    maxrate = max(_MIN_TIER_RATE, int(base_rate * ratio))
    buf_env = (env.get('LIVE_BUFSIZE') or '').strip()
    if buf_env:
        bufsize = max(_MIN_TIER_RATE, int(parse_rate(buf_env) * ratio))
    else:
        bufsize = maxrate * 2
    return maxrate, bufsize


def live_gop(env):
    """Keyframe interval in frames — default one second of frames."""
    gop = (env.get('LIVE_GOP') or '').strip()
    if gop:
        return int(gop)
    return int(env.get('STREAM_FRAMERATE', '30'))


def live_encoder_args(env, use_nvenc, tier_w, tier_h, full_w, full_h):
    """Return the per-tier live encoder argv fragment.

    Capped constant-quality VBR — see the rate-control decision record at
    the top of this file, including the fixed-CBR alternative.
    """
    cq = str(int(env.get('LIVE_CQ', str(DEFAULT_LIVE_CQ))))
    maxrate, bufsize = _tier_rates(env, tier_w, tier_h, full_w, full_h)
    gop = str(live_gop(env))
    if use_nvenc:
        return [
            '-c:v', 'h264_nvenc',
            '-preset', 'p4', '-tune', 'll',
            '-rc', 'vbr', '-cq', cq, '-b:v', '0',
            '-maxrate', str(maxrate), '-bufsize', str(bufsize),
            '-g', gop, '-bf', '0', '-profile:v', 'main',
            '-spatial_aq', '1', '-temporal_aq', '1',
        ]
    return [
        '-c:v', 'libx264',
        '-preset', 'ultrafast', '-tune', 'zerolatency',
        '-crf', cq,
        '-maxrate', str(maxrate), '-bufsize', str(bufsize),
        '-g', gop,
    ]


def _iter_streams(config):
    """Yield (stream_key, crop_or_None, tiers) for every configured stream.

    stream_key 'full' is the whole frame; each screen uses its name.
    crop is (w, h, x, y) for ffmpeg's crop filter.
    """
    yield 'full', None, config['fullTiers']
    for s in config['screens']:
        crop = (s['width'], s['height'], s['x'], s['y'])
        yield s['name'], crop, s.get('tiers') or []


def build_filter_complex(config):
    """Build the filter_complex string and the ordered output branch list.

    Returns (filter_str, branches) where branches is a list of dicts:
      {'label': ffmpeg pad label, 'whepPath': MediaMTX path, ...tier fields}
    plus one final dict {'label': 'arch'} for the archive branch.

    The first stage normalises the capture to the configured WxH and NV12
    once (mirroring the old single cudaconvertscale before the tee), so
    every downstream crop/scale runs on subsampled NV12 instead of BGRA.
    """
    width, height = config['width'], config['height']
    streams = list(_iter_streams(config))

    n_top = len(streams) + 1  # +1 archive
    top_labels = [f'v_{key}' for key, _, _ in streams] + ['v_arch']
    parts = [
        f'[0:v]scale={width}:{height},format=nv12,split={n_top}'
        + ''.join(f'[{lbl}]' for lbl in top_labels)
    ]

    branches = []
    for key, crop, tiers in streams:
        src = f'v_{key}'
        pre = ''
        if crop is not None:
            cw, ch, cx, cy = crop
            pre = f'crop={cw}:{ch}:{cx}:{cy},'
        tier_labels = [f'{key}_t{i}' for i in range(len(tiers))]

        if len(tiers) == 1:
            # Single tier: no split needed.
            chains = [(src, tiers[0], tier_labels[0], pre)]
        else:
            split_outs = [f'{lbl}_raw' for lbl in tier_labels]
            parts.append(
                f'[{src}]{pre}split={len(tiers)}'
                + ''.join(f'[{o}]' for o in split_outs)
            )
            pre = ''  # crop already applied before the split
            chains = [(o, t, lbl, '')
                      for o, t, lbl in zip(split_outs, tiers, tier_labels)]

        for src_lbl, tier, out_lbl, chain_pre in chains:
            base_w = crop[0] if crop else width
            base_h = crop[1] if crop else height
            need_scale = (tier['width'], tier['height']) != (base_w, base_h)
            filters = chain_pre
            if need_scale:
                filters += f'scale={tier["width"]}:{tier["height"]}'
            if filters.endswith(','):
                filters = filters[:-1]
            if filters:
                parts.append(f'[{src_lbl}]{filters}[{out_lbl}]')
            elif src_lbl != out_lbl:
                # Pure passthrough — null filter just renames the pad.
                parts.append(f'[{src_lbl}]null[{out_lbl}]')
            branches.append({'label': out_lbl, **tier})

    return ';'.join(parts), branches


def build_ffmpeg_command(config, env, use_nvenc, archive_args,
                         archive_pattern, segment_list_path,
                         segment_sec, segment_start_number=0):
    """Assemble the full ffmpeg argv for the single capture process.

    archive_args     encoder argv fragment from archive_encoder_args()
    archive_pattern  live-dir sequential output pattern (…/prefix-%05d.mp4)
    """
    display   = env.get('DISPLAY', ':0')
    framerate = env.get('STREAM_FRAMERATE', '30')
    rtsp_port = int(env.get('MEDIAMTX_RTSP_PORT', str(DEFAULT_RTSP_PORT)))
    width, height = config['width'], config['height']

    filter_str, branches = build_filter_complex(config)

    cmd = [
        'ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'warning',
        # x11grab captures the full root window; the graph's first scale
        # normalises to the configured WxH (same semantics as the old
        # videoscale/cudaconvertscale ingress).
        '-f', 'x11grab', '-framerate', framerate, '-i', display,
        '-filter_complex', filter_str,
    ]

    for br in branches:
        cmd += ['-map', f'[{br["label"]}]']
        cmd += live_encoder_args(env, use_nvenc,
                                 br['width'], br['height'], width, height)
        cmd += ['-f', 'rtsp', '-rtsp_transport', 'tcp',
                f'rtsp://127.0.0.1:{rtsp_port}/{br["whepPath"]}']

    # Archive output: fragmented MP4 (moov up front via empty_moov, one
    # fragment per keyframe) so the in-progress file is parseable
    # mid-write; sequential names in the live dir, rotation recorded in
    # the CSV segment list consumed by archive_finalize.py.
    #
    # flush_packets=1 is load-bearing: without it the muxer's 256 KB AVIO
    # buffer means the on-disk active file is just a bare `ftyp` until
    # a quarter-megabyte accumulates — at static-desktop bitrates that is
    # most of a segment's lifetime, and /archive's active-segment serve
    # would find no moov.  With it, ftyp+moov hit the disk when the
    # segment opens and every fragment lands as it completes.  It must
    # live INSIDE segment_format_options: the segment muxer opens its own
    # files, so a top-level -flush_packets never reaches them.
    cmd += ['-map', '[v_arch]']
    cmd += list(archive_args)
    cmd += [
        '-f', 'segment',
        '-segment_time', str(segment_sec),
        '-reset_timestamps', '1',
        '-segment_format', 'mp4',
        '-segment_format_options',
        'movflags=+frag_keyframe+empty_moov+default_base_moof'
        ':flush_packets=1',
        '-segment_list', segment_list_path,
        '-segment_list_type', 'csv',
        '-segment_start_number', str(segment_start_number),
        archive_pattern,
    ]
    return cmd


def build_mediamtx_config(config, env):
    """Render the MediaMTX YAML for this deployment.

    RTSP ingest binds to loopback only (the publisher is the co-located
    ffmpeg); WHEP is served on WEBRTC_PORT.  Every other protocol and the
    API are disabled.  Paths are enumerated explicitly so unknown publish
    or read attempts are rejected.
    """
    rtsp_port  = int(env.get('MEDIAMTX_RTSP_PORT', str(DEFAULT_RTSP_PORT)))
    webrtc     = int(env.get('WEBRTC_PORT', str(DEFAULT_WEBRTC_PORT)))
    webrtc_udp = int(env.get('WEBRTC_UDP_PORT', str(DEFAULT_WEBRTC_UDP)))
    extra_hosts = [
        h.strip() for h in (env.get('WEBRTC_ADDITIONAL_HOSTS') or '').split(',')
        if h.strip()
    ]

    lines = [
        '# Generated by stream_command.py at container start — do not edit.',
        'logLevel: info',
        '',
        'api: no',
        'metrics: no',
        'pprof: no',
        'playback: no',
        '',
        'rtsp: yes',
        f'rtspAddress: 127.0.0.1:{rtsp_port}',
        'rtspTransports: [tcp]',
        '',
        'rtmp: no',
        'hls: no',
        'srt: no',
        '',
        'webrtc: yes',
        f'webrtcAddress: :{webrtc}',
        f'webrtcLocalUDPAddress: :{webrtc_udp}',
        'webrtcIPsFromInterfaces: yes',
    ]
    if extra_hosts:
        lines.append('webrtcAdditionalHosts: ['
                     + ', '.join(extra_hosts) + ']')
    lines += ['', 'paths:']
    for _, _, tiers in _iter_streams(config):
        for t in tiers:
            lines.append(f'  {t["whepPath"]}: {{}}')
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    # Emit the MediaMTX config for the current environment (entrypoint.sh
    # redirects this into /run/desktop-stream/mediamtx.yml).
    import os
    import sys
    from desktop_config import load_config
    sys.stdout.write(build_mediamtx_config(load_config(), os.environ))
