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
    # Latency-tuned preset and gop-size match legacy so we know the encoder
    # negotiates / prerolls the same way it did before quality tuning.
    assert props['preset']      == 'low-latency-hq'
    assert props['gop-size']    == 30
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
    # Latency-tuned preset kept rather than 'lossless-hp', because not every
    # gst-plugins-rs build exposes that preset and we'd rather negotiate
    # against the same preset legacy used and rely on rc-mode/qp-const for
    # the actual lossless behavior.
    assert props['preset']     == 'low-latency-hq'


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


# ── gop_size override propagates everywhere a keyframe interval is set ──────

@pytest.mark.parametrize('quality', VALID_ARCHIVE_QUALITIES)
def test_gop_size_default_is_30(quality):
    """Default gop_size keeps pre-tuning behaviour byte-for-byte."""
    plan = archive_encoder_plan(quality=quality)
    assert _props(plan, 'x264enc')['key-int-max']  == 30
    assert _props(plan, 'nvh264enc')['gop-size']    == 30


@pytest.mark.parametrize('quality', VALID_ARCHIVE_QUALITIES)
def test_gop_size_override_propagates_to_both_encoders(quality):
    """ARCHIVE_GOP_SIZE flows through to both encoders' keyframe-interval knobs.

    nvh264enc spells it `gop-size`; x264enc spells it `key-int-max`; the plan
    sets both off the same gop_size argument so the operator only has to
    think about one number regardless of which encoder ends up running.
    """
    plan = archive_encoder_plan(quality=quality, gop_size=120)
    assert _props(plan, 'x264enc')['key-int-max']  == 120
    assert _props(plan, 'nvh264enc')['gop-size']    == 120


def test_gop_size_does_not_disturb_other_props():
    """Overriding gop_size leaves rate-control, QP and bitrate alone."""
    base   = archive_encoder_plan(quality='visually-lossless', qp=18,
                                  bitrate_cap=100000)
    tuned  = archive_encoder_plan(quality='visually-lossless', qp=18,
                                  bitrate_cap=100000, gop_size=15)

    base_x264   = _props(base,  'x264enc').copy()
    tuned_x264  = _props(tuned, 'x264enc').copy()
    base_nv     = _props(base,  'nvh264enc').copy()
    tuned_nv    = _props(tuned, 'nvh264enc').copy()

    # Pop the keyframe-interval knobs so the rest of the dicts must match.
    assert tuned_x264.pop('key-int-max') == 15
    assert base_x264.pop('key-int-max')  == 30
    assert tuned_nv.pop('gop-size')      == 15
    assert base_nv.pop('gop-size')       == 30

    assert tuned_x264 == base_x264
    assert tuned_nv   == base_nv


# ── Rate-control switch is the only intentional change vs legacy ────────────

@pytest.mark.parametrize('quality', ['visually-lossless', 'lossless'])
def test_new_modes_match_legacy_cpu_profile(quality):
    """The new modes deliberately keep the legacy preset / tune / speed-preset
    triple so the encoder negotiates and prerolls along the same code path.

    An earlier draft of this PR dropped tune=zerolatency and switched to
    speed-preset=4 (faster) / pass=qual / qp-min=0 in the new modes.  CI
    found that the service pipeline got stuck in PAUSED — the archive
    encoder never produced its first buffer, so splitmuxsink never opened
    a segment and `awaitFirstSegment()` timed out.  Until we have a way
    to investigate that interaction (likely qp-min=0 with pass=qual on
    this build of x264enc), the safer move is to keep the latency tunings
    and only flip rate-control mode.
    """
    plan = archive_encoder_plan(quality=quality)
    x264_props = _props(plan, 'x264enc')
    nv_props   = _props(plan, 'nvh264enc')
    assert x264_props['tune']         == 0x4
    assert x264_props['speed-preset'] == 1
    assert x264_props['key-int-max']  == 30
    assert nv_props['preset']         == 'low-latency-hq'
    assert nv_props['gop-size']       == 30
