"""
archive_encoder.py -- pure quality-mode planner for the archive H.264 encoder.

Kept gi-free so unit tests can import it without a working GStreamer install.
pipeline.py wraps the plan returned here in actual element creation +
property setting (via _set_if_present so unknown properties on a particular
encoder build are silently skipped — gst-plugins-rs and -bad ship slightly
different property names across versions).

Quality modes:

  visually-lossless  CRF/CQP at qp (default 18). Indistinguishable from the
                     source on screen content; ~2-4x the file size of the
                     legacy mode.
  lossless           True lossless (QP=0). Files can be very large (50-200
                     Mbps) during heavy motion.
  legacy             Pre-tuning behaviour: fixed-bitrate VBR at the configured
                     ARCHIVE_BITRATE with latency-tuned presets.  Byte-for-byte
                     compatible with the configuration shipped before the
                     archive quality work.

Encoder preference order (per mode):

  nvcudah264enc  CUDA-context-aware NVENC.  Shares the gst-cuda buffer pool
                 with upstream cudaupload/cudaconvertscale, so the handoff
                 into the encoder reuses the same CUDA context without an
                 internal rebind.  Present on gst-plugins-bad >= 1.22.
  nvh264enc      Original NVENC element.  Still consumes CUDAMemory natively
                 but predates the unified CUDA memory model.  Universal
                 fallback when nvcudah264enc isn't registered.
  x264enc        Software fallback.  Used when no NVENC element is present.
"""

VALID_ARCHIVE_QUALITIES = ('visually-lossless', 'lossless', 'legacy')


def archive_encoder_plan(quality='visually-lossless', qp=18, bitrate_legacy=6000):
    """Return ordered [(factory_name, properties_dict), ...] preference list.

    The first entry whose factory exists at runtime wins.  Unknown properties
    on a particular encoder build are skipped at apply time, so we list every
    plausible property name (for example both `qp-const` and `qp-const-i/p/b`
    on nvh264enc) and let the runtime probe sort it out.

    The non-legacy modes deliberately drop `preset=low-latency-hq` on the
    NVENC encoders: the archive has no latency requirement (live viewers go
    through the WebRTC branch), so the encoder is free to use a quality-
    oriented preset and B-frames for better compression at the same QP.
    The x264enc fallback keeps its legacy settings — an earlier attempt to
    relax those caused a CI hang where the encoder never produced its first
    buffer (suspected interaction between qp-min=0 and pass=quant on this
    build of x264enc).
    """
    if quality not in VALID_ARCHIVE_QUALITIES:
        raise ValueError(
            f'unknown ARCHIVE_QUALITY {quality!r}; '
            f'expected one of {VALID_ARCHIVE_QUALITIES}'
        )

    def _nvenc(props):
        """Same property dict for both NVENC variants (they share the API)."""
        return [('nvcudah264enc', dict(props)), ('nvh264enc', dict(props))]

    if quality == 'legacy':
        return _nvenc({
            'preset':      'low-latency-hq',
            'rc-mode':     'vbr-hq',
            'bitrate':     bitrate_legacy,
            'max-bitrate': bitrate_legacy,
            'gop-size':    30,
        }) + [
            ('x264enc', {
                'tune':         0x4,    # zerolatency
                'speed-preset': 1,      # ultrafast
                'bitrate':      bitrate_legacy,
                'key-int-max':  30,
            }),
        ]

    if quality == 'lossless':
        return _nvenc({
            # preset=hq trades a touch of throughput for noticeably better
            # rate-distortion vs low-latency-hq; bframes=2 lets NVENC reuse
            # reference frames for screen content.  Latency cost (~100ms at
            # 30fps) is irrelevant for the archive path.
            'preset':      'hq',
            'rc-mode':     'constqp',
            'qp-const':    0,
            'qp-const-i':  0,
            'qp-const-p':  0,
            'qp-const-b':  0,
            'bframes':     2,
            'gop-size':    30,
        }) + [
            ('x264enc', {
                'tune':         0x4,    # zerolatency (match legacy CPU profile)
                'speed-preset': 1,      # ultrafast (match legacy CPU profile)
                'pass':         4,      # 4 = quant (constant quantizer)
                'quantizer':    0,
                'qp-min':       0,      # required for true QP=0 lossless
                'qp-max':       0,
                'key-int-max':  30,
            }),
        ]

    # visually-lossless
    return _nvenc({
        'preset':      'hq',
        'rc-mode':     'constqp',
        'qp-const':    qp,
        'qp-const-i':  qp,
        'qp-const-p':  qp,
        'qp-const-b':  qp,
        'bframes':     2,
        'gop-size':    30,
    }) + [
        ('x264enc', {
            'tune':         0x4,        # zerolatency (match legacy CPU profile)
            'speed-preset': 1,          # ultrafast (match legacy CPU profile)
            'pass':         4,          # 4 = quant (constant quantizer at QP)
            'quantizer':    qp,
            'key-int-max':  30,
        }),
    ]
