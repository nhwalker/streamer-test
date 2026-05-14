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
"""

VALID_ARCHIVE_QUALITIES = ('visually-lossless', 'lossless', 'legacy')


def archive_encoder_plan(quality='visually-lossless', qp=18,
                         bitrate_cap=100000, bitrate_legacy=6000,
                         gop_size=30):
    """Return ordered [(factory_name, properties_dict), ...] preference list.

    The first entry whose factory exists at runtime wins.  Unknown properties
    on a particular encoder build are skipped at apply time, so we list every
    plausible property name (for example both `qp-const` and `qp-const-i/p/b`
    on nvh264enc) and let the runtime probe sort it out.

    `gop_size` controls the maximum keyframe interval in frames — applied as
    `gop-size` on nvh264enc and `key-int-max` on x264enc.  Smaller values
    bound the seek/recovery interval at the cost of compression efficiency.

    Latency tunings (`tune=zerolatency`, `preset=low-latency-hq`) are
    deliberately absent from the new modes — viewers see the screen via the
    WebRTC branch, so the archive encoder doesn't owe anyone fast keyframes.
    """
    if quality not in VALID_ARCHIVE_QUALITIES:
        raise ValueError(
            f'unknown ARCHIVE_QUALITY {quality!r}; '
            f'expected one of {VALID_ARCHIVE_QUALITIES}'
        )

    if quality == 'legacy':
        return [
            ('nvh264enc', {
                'preset':      'low-latency-hq',
                'rc-mode':     'vbr-hq',
                'bitrate':     bitrate_legacy,
                'max-bitrate': bitrate_legacy,
                'gop-size':    gop_size,
            }),
            ('x264enc', {
                'tune':         0x4,    # zerolatency
                'speed-preset': 1,      # ultrafast
                'bitrate':      bitrate_legacy,
                'key-int-max':  gop_size,
            }),
        ]

    if quality == 'lossless':
        return [
            ('nvh264enc', {
                # Latency-tuned preset is kept; the encoder still has no real
                # latency requirement on the archive path, but gst-plugins-rs
                # builds on different hosts disagree on which other presets
                # exist (e.g. 'lossless-hp' is absent on some), and matching
                # the legacy preset means we know the encoder negotiates and
                # prerolls correctly.  rc-mode=constqp + qp-const=0 is the
                # actual lossless switch.
                'preset':      'low-latency-hq',
                'rc-mode':     'constqp',
                'qp-const':    0,
                'qp-const-i':  0,
                'qp-const-p':  0,
                'qp-const-b':  0,
                'gop-size':    gop_size,
            }),
            ('x264enc', {
                'tune':         0x4,    # zerolatency (match legacy CPU profile)
                'speed-preset': 1,      # ultrafast (match legacy CPU profile)
                'pass':         4,      # 4 = quant (constant quantizer)
                'quantizer':    0,
                'qp-min':       0,      # required for true QP=0 lossless
                'qp-max':       0,
                'key-int-max':  gop_size,
            }),
        ]

    # visually-lossless
    return [
        ('nvh264enc', {
            # Same preset and gop-size as legacy mode — the only real change
            # is rc-mode/qp-const, which redirects rate control from bitrate-
            # targeted (vbr-hq) to quality-targeted (constqp).  Latency-tuned
            # preset is kept for build-version compatibility (see the lossless
            # comment above).
            'preset':      'low-latency-hq',
            'rc-mode':     'constqp',
            'qp-const':    qp,
            'qp-const-i':  qp,
            'qp-const-p':  qp,
            'qp-const-b':  qp,
            'gop-size':    gop_size,
            'max-bitrate': bitrate_cap,
        }),
        ('x264enc', {
            'tune':         0x4,        # zerolatency (match legacy CPU profile)
            'speed-preset': 1,          # ultrafast (match legacy CPU profile)
            'pass':         4,          # 4 = quant (constant quantizer at QP)
            'quantizer':    qp,
            'key-int-max':  gop_size,
        }),
    ]
