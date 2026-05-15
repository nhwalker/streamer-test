"""
Unit tests for archive_encoder.archive_encoder_plan().

Pure-function tests; no GStreamer / Docker / X server required.  They lock
the property dicts that pipeline.py applies to nvh264enc / x264enc for each
quality mode.  When a property is renamed in a future encoder build the
pipeline silently skips it via _set_if_present, so these tests are the
contract that says "we tried" — they fail when someone changes a mode's
intent (e.g. drops qp-min=0 from lossless).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
from archive_encoder import archive_encoder_plan, VALID_ARCHIVE_QUALITIES  # noqa: E402


def _props(plan, factory):
    """Return the properties dict for `factory` from the plan, or fail."""
    for name, props in plan:
        if name == factory:
            return props
    raise AssertionError(f'{factory} not in plan: {[n for n, _ in plan]}')


# ── Mode-presence + ordering ────────────────────────────────────────────────

def test_default_mode_is_visually_lossless():
    # Default args == visually-lossless preset; lock that as the package default.
    plan = archive_encoder_plan()
    assert _props(plan, 'x264enc')['quantizer'] == 18
    assert _props(plan, 'nvh264enc')['qp-const'] == 18


@pytest.mark.parametrize('quality', VALID_ARCHIVE_QUALITIES)
def test_every_mode_offers_both_encoders(quality):
    """NVENC variants preferred (cuda-aware first), x264enc as fallback."""
    plan = archive_encoder_plan(quality=quality)
    factories = [name for name, _ in plan]
    assert factories == ['nvcudah264enc', 'nvh264enc', 'x264enc'], (
        f'{quality}: expected nvcudah264enc -> nvh264enc -> x264enc ordering'
    )


@pytest.mark.parametrize('quality', VALID_ARCHIVE_QUALITIES)
def test_nvenc_variants_share_property_dict(quality):
    """Both NVENC variants must apply the same configuration — they share
    the property API and the runtime picks whichever is registered."""
    plan = archive_encoder_plan(quality=quality)
    assert _props(plan, 'nvcudah264enc') == _props(plan, 'nvh264enc')


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match='ARCHIVE_QUALITY'):
        archive_encoder_plan(quality='maximum-overdrive')


# ── visually-lossless: the default tier ─────────────────────────────────────

def test_visually_lossless_x264enc_uses_cqp_at_qp():
    props = _props(archive_encoder_plan(quality='visually-lossless', qp=18),
                   'x264enc')
    # pass=4 == quant (constant quantizer); quantizer is the CQP value.
    assert props['pass']      == 4
    assert props['quantizer'] == 18
    # Latency-tuned preset and tune are kept so the encoder negotiates and
    # prerolls the same way it did in legacy mode — the only quality change
    # is rate-control mode.  CI flushed out a hang when these were dropped.
    assert props['tune']         == 0x4
    assert props['speed-preset'] == 1
    assert props['key-int-max']  == 30
    # qp-min / qp-max are NOT set in this mode — left at encoder defaults.
    # Setting qp-min=0 only matters for true lossless and may interact badly
    # with pass=quant on some builds.
    assert 'qp-min' not in props
    assert 'qp-max' not in props


def test_visually_lossless_nvh264enc_uses_constqp_at_qp():
    props = _props(archive_encoder_plan(quality='visually-lossless', qp=18),
                   'nvh264enc')
    assert props['rc-mode'] == 'constqp'
    # Probe both spellings — different gst-plugins versions expose different ones.
    assert props['qp-const']   == 18
    assert props['qp-const-i'] == 18
    assert props['qp-const-p'] == 18
    assert props['qp-const-b'] == 18
    # Archive has no latency requirement (live viewers go via WebRTC), so the
    # encoder switches to the high-quality preset with B-frames for better
    # compression at the same QP.
    assert props['preset']      == 'hq'
    assert props['bframes']     == 2
    assert props['gop-size']    == 30
    # max-bitrate is intentionally absent: NVENC ignores it under
    # rc-mode=constqp (the property is only honored in vbr/cbr modes), so
    # advertising a "cap" that doesn't apply was misleading.
    assert 'max-bitrate' not in props


def test_visually_lossless_qp_override_propagates_to_both_encoders():
    plan = archive_encoder_plan(quality='visually-lossless', qp=22)
    assert _props(plan, 'x264enc')['quantizer']    == 22
    assert _props(plan, 'nvh264enc')['qp-const']   == 22
    assert _props(plan, 'nvh264enc')['qp-const-p'] == 22


# ── lossless: opt-in true lossless ──────────────────────────────────────────

def test_lossless_x264enc_pins_qp_to_zero():
    props = _props(archive_encoder_plan(quality='lossless'), 'x264enc')
    assert props['pass']      == 4    # quant — true CQP at QP=0
    assert props['quantizer'] == 0
    # qp-min=0 and qp-max=0 are required so the encoder cannot wander above 0.
    assert props['qp-min']    == 0
    assert props['qp-max']    == 0
    # Latency-tuned preset and tune are kept (same rationale as
    # visually-lossless): preserve the legacy preroll path.
    assert props['tune']         == 0x4
    assert props['speed-preset'] == 1
    assert props['key-int-max']  == 30


def test_lossless_nvh264enc_pins_qp_to_zero():
    props = _props(archive_encoder_plan(quality='lossless'), 'nvh264enc')
    # All QP knobs at 0 (so whichever path the encoder consults yields lossless)
    assert props['qp-const']   == 0
    assert props['qp-const-i'] == 0
    assert props['qp-const-p'] == 0
    assert props['qp-const-b'] == 0
    assert props['rc-mode']    == 'constqp'
    # High-quality preset + B-frames: the archive has no latency requirement
    # so the encoder is free to use the slower preset for better compression.
    # 'hq' is the conservative choice over 'lossless'/'lossless-hp' which
    # aren't exposed on every gst-plugins-bad build.
    assert props['preset']     == 'hq'
    assert props['bframes']    == 2


def test_lossless_ignores_qp_override():
    """The QP override knob is only meaningful in visually-lossless mode."""
    props = _props(archive_encoder_plan(quality='lossless', qp=22), 'x264enc')
    assert props['quantizer'] == 0


# ── legacy: byte-for-byte compat with pre-tuning behaviour ──────────────────

def test_legacy_x264enc_matches_pre_tuning_config():
    props = _props(archive_encoder_plan(quality='legacy', bitrate_legacy=6000),
                   'x264enc')
    assert props == {
        'tune':         0x4,    # zerolatency
        'speed-preset': 1,      # ultrafast
        'bitrate':      6000,
        'key-int-max':  30,
    }


def test_legacy_nvh264enc_matches_pre_tuning_config():
    props = _props(archive_encoder_plan(quality='legacy', bitrate_legacy=6000),
                   'nvh264enc')
    assert props == {
        'preset':      'low-latency-hq',
        'rc-mode':     'vbr-hq',
        'bitrate':     6000,
        'max-bitrate': 6000,
        'gop-size':    30,
    }


def test_legacy_bitrate_override_propagates():
    plan = archive_encoder_plan(quality='legacy', bitrate_legacy=12000)
    assert _props(plan, 'x264enc')['bitrate']      == 12000
    assert _props(plan, 'nvh264enc')['bitrate']    == 12000
    assert _props(plan, 'nvh264enc')['max-bitrate'] == 12000


# ── Rate-control switch is the only intentional change vs legacy ────────────

@pytest.mark.parametrize('quality', ['visually-lossless', 'lossless'])
def test_x264_fallback_keeps_legacy_latency_profile(quality):
    """x264enc deliberately keeps the legacy tune / speed-preset triple.

    An earlier draft dropped tune=zerolatency and switched to
    speed-preset=4 (faster) / pass=qual / qp-min=0 in the new modes.  CI
    found that the service pipeline got stuck in PAUSED — the archive
    encoder never produced its first buffer, so splitmuxsink never opened
    a segment and `awaitFirstSegment()` timed out.  The suspected
    interaction (qp-min=0 with pass=qual on this build of x264enc) was
    never fully understood, so x264enc stays on the legacy negotiation
    path; the NVENC encoders are free to take the slower presets because
    that path was never implicated.
    """
    plan = archive_encoder_plan(quality=quality)
    x264_props = _props(plan, 'x264enc')
    assert x264_props['tune']         == 0x4
    assert x264_props['speed-preset'] == 1
    assert x264_props['key-int-max']  == 30


@pytest.mark.parametrize('quality', ['visually-lossless', 'lossless'])
def test_nvenc_modes_drop_legacy_latency_preset(quality):
    """NVENC archive encoders use the high-quality preset; the archive has no
    latency requirement (live viewers go through the WebRTC branch)."""
    plan = archive_encoder_plan(quality=quality)
    for factory in ('nvcudah264enc', 'nvh264enc'):
        nv_props = _props(plan, factory)
        assert nv_props['preset']   == 'hq'
        assert nv_props['bframes']  == 2
        assert nv_props['gop-size'] == 30
