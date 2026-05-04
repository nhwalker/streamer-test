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
                         bitrate_cap=100000, bitrate_legacy=6000):
    """Return ordered [(factory_name, properties_dict), ...] preference list.

    The first entry whose factory exists at runtime wins.  Unknown properties
    on a particular encoder build are skipped at apply time, so we list every
    plausible property name (for example both `qp-const` and `qp-const-i/p/b`
    on nvh264enc) and let the runtime probe sort it out.

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
                'gop-size':    30,
            }),
            ('x264enc', {
                'tune':         0x4,    # zerolatency
                'speed-preset': 1,      # ultrafast
                'bitrate':      bitrate_legacy,
                'key-int-max':  30,
            }),
        ]

    if quality == 'lossless':
        return [
            ('nvh264enc', {
                # Try the named preset first; on builds that don't expose it,
                # the rc-mode + qp-const path below still produces lossless.
                'preset':      'lossless-hp',
                'rc-mode':     'constqp',
                'qp-const':    0,
                'qp-const-i':  0,
                'qp-const-p':  0,
                'qp-const-b':  0,
                'gop-size':    60,
            }),
            ('x264enc', {
                'pass':         4,      # 4 = quant (constant quantizer)
                'quantizer':    0,
                'qp-min':       0,
                'qp-max':       0,
                'speed-preset': 4,      # faster
                'key-int-max':  60,
            }),
        ]

    # visually-lossless
    return [
        ('nvh264enc', {
            'rc-mode':     'constqp',
            'qp-const':    qp,
            'qp-const-i':  qp,
            'qp-const-p':  qp,
            'qp-const-b':  qp,
            'preset':      'high-quality',
            'gop-size':    60,
            'max-bitrate': bitrate_cap,
        }),
        ('x264enc', {
            'pass':         5,          # 5 = qual (CRF)
            'quantizer':    qp,
            'qp-min':       0,
            'qp-max':       51,
            'speed-preset': 4,          # faster
            'key-int-max':  60,
        }),
    ]
