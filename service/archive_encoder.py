"""
archive_encoder.py -- pure quality-mode planner for the archive H.264 encoder.

Returns ffmpeg argv fragments (consumed by stream_command.build_ffmpeg_command)
so unit tests can assert the exact encoder configuration without ffmpeg or a
GPU present.  pipeline.py decides `use_nvenc` once at startup by probing
h264_nvenc with a one-frame test encode.

Quality modes:

  visually-lossless  Constant QP at `qp` (default 18). Indistinguishable from
                     the source on screen content; ~2-4x the file size of the
                     legacy mode.
  lossless           True lossless.  On NVENC this uses the encoder's
                     dedicated lossless tune (equivalent intent to the old
                     QP=0 configuration); on libx264 it is -qp 0.  Files can
                     be very large (50-200 Mbps) during heavy motion.
  legacy             Pre-tuning behaviour: fixed-bitrate VBR at the configured
                     ARCHIVE_BITRATE with latency-tuned presets.

The non-legacy NVENC modes deliberately use a quality-oriented preset and
B-frames: the archive has no latency requirement (live viewers go through
the MediaMTX branch), so the encoder is free to trade latency for better
compression at the same QP.  The libx264 fallback keeps the ultrafast/
zerolatency profile so a GPU-less host can still encode the archive in
real time alongside the live tiers.
"""

VALID_ARCHIVE_QUALITIES = ('visually-lossless', 'lossless', 'legacy')


def archive_encoder_args(quality='visually-lossless', qp=18,
                         bitrate_legacy=6000, use_nvenc=True, gop=30):
    """Return the ffmpeg encoder argv fragment for the archive output.

    gop is the keyframe interval in frames; one keyframe per second keeps
    every fMP4 fragment one GOP long (frag_keyframe fragments at each
    keyframe), matching the old mp4mux fragment-duration=1000 layout.
    """
    if quality not in VALID_ARCHIVE_QUALITIES:
        raise ValueError(
            f'unknown ARCHIVE_QUALITY {quality!r}; '
            f'expected one of {VALID_ARCHIVE_QUALITIES}'
        )
    gop = str(int(gop))

    if use_nvenc:
        if quality == 'legacy':
            kbps = int(bitrate_legacy)
            return [
                '-c:v', 'h264_nvenc',
                '-preset', 'p4', '-tune', 'll',
                '-rc', 'vbr',
                '-b:v', f'{kbps}k', '-maxrate', f'{kbps}k',
                '-bufsize', f'{2 * kbps}k',
                '-g', gop,
            ]
        if quality == 'lossless':
            return [
                '-c:v', 'h264_nvenc',
                '-preset', 'p6', '-tune', 'lossless',
                '-g', gop,
            ]
        # visually-lossless
        return [
            '-c:v', 'h264_nvenc',
            '-preset', 'p6',
            '-rc', 'constqp', '-qp', str(int(qp)),
            '-bf', '2',
            '-g', gop,
        ]

    # libx264 fallback — keep the real-time-safe profile in every mode.
    base = ['-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency']
    if quality == 'legacy':
        kbps = int(bitrate_legacy)
        return base + ['-b:v', f'{kbps}k', '-g', gop]
    if quality == 'lossless':
        return base + ['-qp', '0', '-g', gop]
    return base + ['-qp', str(int(qp)), '-g', gop]
