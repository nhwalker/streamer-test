"""
Unit tests for archive_encoder.archive_encoder_args().

Pure-function tests; no ffmpeg / Docker / X server required.  They lock the
ffmpeg argv fragments pipeline.py appends to the archive output for each
quality mode — they fail when someone changes a mode's intent (e.g. drops
the lossless tune or the constant-QP rate control).
"""

import pytest

from archive_encoder import archive_encoder_args, VALID_ARCHIVE_QUALITIES


def _opt(args, name):
    """Return the value following option `name` in an argv fragment."""
    for i, a in enumerate(args[:-1]):
        if a == name:
            return args[i + 1]
    raise AssertionError(f'{name} not in args: {args}')


def _codec(args):
    return _opt(args, '-c:v')


# ── Mode selection ───────────────────────────────────────────────────────────

def test_default_mode_is_visually_lossless():
    args = archive_encoder_args(use_nvenc=True)
    assert _codec(args) == 'h264_nvenc'
    assert _opt(args, '-rc') == 'constqp'
    assert _opt(args, '-qp') == '18'


@pytest.mark.parametrize('quality', VALID_ARCHIVE_QUALITIES)
@pytest.mark.parametrize('use_nvenc', [True, False])
def test_every_mode_has_both_encoder_paths(quality, use_nvenc):
    args = archive_encoder_args(quality=quality, use_nvenc=use_nvenc)
    assert _codec(args) == ('h264_nvenc' if use_nvenc else 'libx264')


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match='ARCHIVE_QUALITY'):
        archive_encoder_args(quality='maximum-overdrive')


# ── visually-lossless: the default tier ─────────────────────────────────────

def test_visually_lossless_nvenc_uses_constqp_at_qp():
    args = archive_encoder_args(quality='visually-lossless', qp=18,
                                use_nvenc=True)
    assert _opt(args, '-rc') == 'constqp'
    assert _opt(args, '-qp') == '18'
    # Archive has no latency requirement (live viewers go via MediaMTX), so
    # the encoder uses a quality-oriented preset with B-frames for better
    # compression at the same QP.
    assert _opt(args, '-preset') == 'p6'
    assert _opt(args, '-bf') == '2'


def test_visually_lossless_x264_uses_qp():
    args = archive_encoder_args(quality='visually-lossless', qp=18,
                                use_nvenc=False)
    assert _opt(args, '-qp') == '18'
    # The CPU fallback keeps the real-time-safe profile so a GPU-less host
    # can archive alongside the live encodes.
    assert _opt(args, '-preset') == 'ultrafast'
    assert _opt(args, '-tune') == 'zerolatency'


@pytest.mark.parametrize('use_nvenc', [True, False])
def test_visually_lossless_qp_override_propagates(use_nvenc):
    args = archive_encoder_args(quality='visually-lossless', qp=22,
                                use_nvenc=use_nvenc)
    assert _opt(args, '-qp') == '22'


# ── lossless: opt-in true lossless ──────────────────────────────────────────

def test_lossless_nvenc_uses_lossless_tune():
    args = archive_encoder_args(quality='lossless', use_nvenc=True)
    assert _opt(args, '-tune') == 'lossless'
    # constqp/-qp must NOT be present: the dedicated lossless tune is the
    # authoritative mode on NVENC (QP-0 constqp is not guaranteed lossless).
    assert '-rc' not in args
    assert '-qp' not in args


def test_lossless_x264_pins_qp_to_zero():
    args = archive_encoder_args(quality='lossless', use_nvenc=False)
    assert _opt(args, '-qp') == '0'


def test_lossless_ignores_qp_override():
    """The QP override knob is only meaningful in visually-lossless mode."""
    args = archive_encoder_args(quality='lossless', qp=22, use_nvenc=False)
    assert _opt(args, '-qp') == '0'


# ── legacy: fixed-bitrate VBR compat ─────────────────────────────────────────

def test_legacy_nvenc_uses_fixed_bitrate_vbr():
    args = archive_encoder_args(quality='legacy', bitrate_legacy=6000,
                                use_nvenc=True)
    assert _opt(args, '-rc') == 'vbr'
    assert _opt(args, '-b:v') == '6000k'
    assert _opt(args, '-maxrate') == '6000k'
    # Latency-tuned preset mirrors the old low-latency-hq configuration.
    assert _opt(args, '-tune') == 'll'


def test_legacy_x264_uses_fixed_bitrate():
    args = archive_encoder_args(quality='legacy', bitrate_legacy=6000,
                                use_nvenc=False)
    assert _opt(args, '-b:v') == '6000k'
    assert _opt(args, '-tune') == 'zerolatency'


@pytest.mark.parametrize('use_nvenc', [True, False])
def test_legacy_bitrate_override_propagates(use_nvenc):
    args = archive_encoder_args(quality='legacy', bitrate_legacy=12000,
                                use_nvenc=use_nvenc)
    assert _opt(args, '-b:v') == '12000k'


# ── GOP alignment ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('quality', VALID_ARCHIVE_QUALITIES)
@pytest.mark.parametrize('use_nvenc', [True, False])
def test_gop_propagates_to_every_mode(quality, use_nvenc):
    """One keyframe per GOP keeps each fMP4 fragment one GOP long
    (frag_keyframe fragments at keyframes) and bounds segment-cut skew."""
    args = archive_encoder_args(quality=quality, use_nvenc=use_nvenc, gop=60)
    assert _opt(args, '-g') == '60'
