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
    """nvh264enc must come first (preferred) and x264enc must be a fallback."""
    plan = archive_encoder_plan(quality=quality)
    factories = [name for name, _ in plan]
    assert factories == ['nvh264enc', 'x264enc'], (
        f'{quality}: expected nvh264enc preferred + x264enc fallback'
    )


def test_unknown_mode_raises():
    with pytest.raises(ValueError, match='ARCHIVE_QUALITY'):
        archive_encoder_plan(quality='maximum-overdrive')


# ── visually-lossless: the default tier ─────────────────────────────────────

def test_visually_lossless_x264enc_uses_crf_at_qp():
    props = _props(archive_encoder_plan(quality='visually-lossless', qp=18),
                   'x264enc')
    # pass=5 == qual (CRF), and quantizer is the CRF value.
    assert props['pass']      == 5
    assert props['quantizer'] == 18
    # QP range left wide open so the encoder can pick whatever it needs.
    assert props['qp-min']    == 0
    assert props['qp-max']    == 51
    # speed-preset 4 (faster) — better compression than today's ultrafast(1)
    # since this hop has no latency requirement.
    assert props['speed-preset'] == 4
    # No tune=zerolatency — that's a legacy-mode setting.
    assert 'tune' not in props


def test_visually_lossless_nvh264enc_uses_constqp_at_qp():
    props = _props(archive_encoder_plan(quality='visually-lossless', qp=18),
                   'nvh264enc')
    assert props['rc-mode'] == 'constqp'
    # Probe both spellings — different gst-plugins versions expose different ones.
    assert props['qp-const']   == 18
    assert props['qp-const-i'] == 18
    assert props['qp-const-p'] == 18
    assert props['qp-const-b'] == 18
    # max-bitrate caps a chaotic frame so we never blow up segment size.
    assert props['max-bitrate'] == 100000


def test_visually_lossless_qp_override_propagates_to_both_encoders():
    plan = archive_encoder_plan(quality='visually-lossless', qp=22)
    assert _props(plan, 'x264enc')['quantizer']    == 22
    assert _props(plan, 'nvh264enc')['qp-const']   == 22
    assert _props(plan, 'nvh264enc')['qp-const-p'] == 22


def test_visually_lossless_bitrate_cap_overrides():
    plan = archive_encoder_plan(quality='visually-lossless',
                                bitrate_cap=25000)
    assert _props(plan, 'nvh264enc')['max-bitrate'] == 25000


# ── lossless: opt-in true lossless ──────────────────────────────────────────

def test_lossless_x264enc_pins_qp_to_zero():
    props = _props(archive_encoder_plan(quality='lossless'), 'x264enc')
    assert props['pass']      == 4    # quant — true CQP at QP=0
    assert props['quantizer'] == 0
    assert props['qp-min']    == 0
    assert props['qp-max']    == 0
    assert 'tune' not in props


def test_lossless_nvh264enc_pins_qp_to_zero():
    props = _props(archive_encoder_plan(quality='lossless'), 'nvh264enc')
    # All QP knobs at 0 (so whichever path the encoder consults yields lossless)
    assert props['qp-const']   == 0
    assert props['qp-const-i'] == 0
    assert props['qp-const-p'] == 0
    assert props['qp-const-b'] == 0
    assert props['rc-mode']    == 'constqp'


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


# ── No latency tunings on the new modes ─────────────────────────────────────

@pytest.mark.parametrize('quality', ['visually-lossless', 'lossless'])
def test_new_modes_drop_latency_tunings(quality):
    """Archive isn't a latency-sensitive path — viewers go through WebRTC."""
    plan = archive_encoder_plan(quality=quality)
    x264_props = _props(plan, 'x264enc')
    nv_props   = _props(plan, 'nvh264enc')
    # x264enc: no zerolatency tune, slower preset.
    assert 'tune' not in x264_props
    assert x264_props['speed-preset'] != 1
    # nvh264enc: no low-latency preset.
    assert nv_props.get('preset') != 'low-latency-hq'
