"""
Unit tests for stream_command.py — the pure ffmpeg-argv / MediaMTX-config
builders.  No ffmpeg, Docker, or X server required.
"""

import pytest

from desktop_config import compute_config
from stream_command import (
    build_ffmpeg_command,
    build_filter_complex,
    build_mediamtx_config,
    live_encoder_args,
    live_gop,
    parse_rate,
)


def _cfg(**env):
    base = {'STREAM_WIDTH': '1920', 'STREAM_HEIGHT': '1080',
            'DESKTOP_SPLITS': '1920x540+0+0;1920x540+0+540',
            'LIVE_SCALE_LADDER': '1.0,0.5'}
    base.update(env)
    return compute_config(base), base


def _cmd(cfg, env, use_nvenc=True, archive_args=('-c:v', 'x'), **kw):
    kw.setdefault('archive_pattern', '/live/stream-%05d.mp4')
    kw.setdefault('segment_list_path', '/live/segments.csv')
    kw.setdefault('segment_sec', 600)
    return build_ffmpeg_command(cfg, env, use_nvenc, list(archive_args), **kw)


# ── parse_rate ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw,expected', [
    ('8M', 8_000_000), ('6000k', 6_000_000), ('500000', 500_000),
    ('1.5M', 1_500_000), ('  2m ', 2_000_000),
])
def test_parse_rate(raw, expected):
    assert parse_rate(raw) == expected


@pytest.mark.parametrize('raw', ['', 'abc', 'M'])
def test_parse_rate_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_rate(raw)


# ── live encoder args ────────────────────────────────────────────────────────

def test_live_nvenc_is_capped_constant_quality():
    args = live_encoder_args({}, True, 1920, 1080, 1920, 1080)
    s = ' '.join(args)
    # Capped VBR at constant quality — the decision-record default.
    assert '-rc vbr' in s
    assert '-cq 18' in s
    assert '-maxrate 8000000' in s
    assert '-bufsize 16000000' in s
    # NVENC path, no B-frames (latency), 1s GOP at the default 30 fps.
    assert '-c:v h264_nvenc' in s
    assert '-bf 0' in s
    assert '-g 30' in s


def test_live_x264_fallback_uses_crf_with_cap():
    args = live_encoder_args({}, False, 1920, 1080, 1920, 1080)
    s = ' '.join(args)
    assert '-c:v libx264' in s
    assert '-crf 18' in s
    assert '-maxrate 8000000' in s
    assert '-tune zerolatency' in s


def test_live_cq_and_maxrate_env_overrides():
    env = {'LIVE_CQ': '23', 'LIVE_MAXRATE': '4M'}
    args = live_encoder_args(env, True, 1920, 1080, 1920, 1080)
    s = ' '.join(args)
    assert '-cq 23' in s
    assert '-maxrate 4000000' in s


def test_smaller_tiers_get_proportionally_smaller_caps():
    # Half-scale tier = quarter the pixels = quarter the cap.
    args = live_encoder_args({}, True, 960, 540, 1920, 1080)
    assert '-maxrate 2000000' in ' '.join(args)


def test_tier_cap_never_drops_below_floor():
    args = live_encoder_args({}, True, 64, 64, 3840, 2160)
    s = ' '.join(args)
    assert '-maxrate 500000' in s


def test_live_gop_defaults_to_one_second_and_is_overridable():
    assert live_gop({}) == 30
    assert live_gop({'STREAM_FRAMERATE': '60'}) == 60
    assert live_gop({'LIVE_GOP': '15', 'STREAM_FRAMERATE': '60'}) == 15


# ── filter graph ─────────────────────────────────────────────────────────────

def test_filter_complex_normalises_once_then_fans_out():
    cfg, _ = _cfg()
    filt, branches = build_filter_complex(cfg)
    # One scale+convert at the head (like the old single cudaconvertscale),
    # split into full + 2 screens + archive.
    assert filt.startswith('[0:v]scale=1920:1080,format=nv12,split=4')
    assert '[v_arch]' in filt
    # 3 streams x 2 tiers of live branches.
    assert [b['whepPath'] for b in branches] == [
        'full_t0', 'full_t1', 'top_t0', 'top_t1', 'bottom_t0', 'bottom_t1',
    ]


def test_filter_complex_crops_screens_once_before_tier_split():
    cfg, _ = _cfg()
    filt, _ = build_filter_complex(cfg)
    assert '[v_top]crop=1920:540:0:0,split=2' in filt
    assert '[v_bottom]crop=1920:540:0:540,split=2' in filt
    # Crop must appear exactly once per screen (before the tier split),
    # not once per tier.
    assert filt.count('crop=1920:540:0:540') == 1


def test_filter_complex_archive_disabled_omits_arch_pad():
    cfg, _ = _cfg()
    filt, branches = build_filter_complex(cfg, archive=False)
    assert '[v_arch]' not in filt
    # Fan-out shrinks by one: full + 2 screens, no archive leg.
    assert filt.startswith('[0:v]scale=1920:1080,format=nv12,split=3')
    # Live branches are unchanged.
    assert [b['whepPath'] for b in branches] == [
        'full_t0', 'full_t1', 'top_t0', 'top_t1', 'bottom_t0', 'bottom_t1',
    ]


def test_filter_complex_single_tier_skips_split():
    cfg, _ = _cfg(LIVE_SCALE_LADDER='1.0')
    filt, branches = build_filter_complex(cfg)
    assert [b['whepPath'] for b in branches] == ['full_t0', 'top_t0', 'bottom_t0']
    # Inner tier splits absent; only the top-level fan-out split remains.
    assert filt.count('split=') == 1


# ── full command ─────────────────────────────────────────────────────────────

def test_command_publishes_every_tier_to_rtsp_over_tcp():
    cfg, env = _cfg()
    cmd = _cmd(cfg, env)
    s = ' '.join(cmd)
    for path in ('full_t0', 'full_t1', 'top_t0', 'top_t1',
                 'bottom_t0', 'bottom_t1'):
        assert f'rtsp://127.0.0.1:8554/{path}' in s
    assert s.count('-rtsp_transport tcp') == 6


def test_command_rtsp_port_override():
    cfg, env = _cfg(MEDIAMTX_RTSP_PORT='8564')
    assert 'rtsp://127.0.0.1:8564/full_t0' in ' '.join(_cmd(cfg, env))


def test_command_archive_writes_readable_fragmented_mp4():
    cfg, env = _cfg()
    cmd = _cmd(cfg, env, archive_args=['-c:v', 'h264_nvenc', '-qp', '18'])
    s = ' '.join(cmd)
    # In-progress readability needs BOTH empty_moov (moov atom up front)
    # AND per-packet flushing — without flush_packets the muxer's 256 KB
    # AVIO buffer keeps the on-disk active file at a bare `ftyp` for most
    # of a low-bitrate segment's lifetime.  frag_keyframe keeps one
    # fragment per GOP.
    assert ('movflags=+frag_keyframe+empty_moov+default_base_moof'
            ':flush_packets=1') in s
    assert '-segment_time 600' in s
    assert '-segment_list /live/segments.csv' in s
    assert cmd[-1] == '/live/stream-%05d.mp4'


def test_command_archive_args_none_disables_archive_output():
    cfg, env = _cfg()
    cmd = build_ffmpeg_command(
        cfg, env, True, None,
        archive_pattern='/live/stream-%05d.mp4',
        segment_list_path='/live/segments.csv',
        segment_sec=600,
    )
    s = ' '.join(cmd)
    # No archive branch anywhere: no pad, no segment muxer, no outputs
    # under the live dir.
    assert 'v_arch' not in s
    assert '-f segment' not in s
    assert '/live/' not in s
    # Live tiers still publish as usual; the last output is now RTSP.
    assert s.count('-rtsp_transport tcp') == 6
    assert cmd[-1].startswith('rtsp://127.0.0.1:8554/')


def test_command_segment_start_number_propagates():
    cfg, env = _cfg()
    cmd = _cmd(cfg, env, segment_start_number=17)
    s = ' '.join(cmd)
    assert '-segment_start_number 17' in s


def test_command_captures_x11_display_from_env():
    cfg, env = _cfg(DISPLAY=':7', STREAM_FRAMERATE='25')
    cmd = _cmd(cfg, env)
    s = ' '.join(cmd)
    assert '-f x11grab -framerate 25 -i :7' in s


# ── MediaMTX config ──────────────────────────────────────────────────────────

def test_mediamtx_config_binds_rtsp_to_loopback_only():
    cfg, env = _cfg()
    yml = build_mediamtx_config(cfg, env)
    assert 'rtspAddress: 127.0.0.1:8554' in yml


def test_mediamtx_config_disables_everything_but_rtsp_and_webrtc():
    # Default-on protocols left unlisted keep their default listeners
    # bound — two instances on one host then collide on those ports and
    # the second dies at startup (this bit us with MoQ's :8892).  Every
    # protocol MediaMTX ships enabled must appear here as 'no'.
    cfg, env = _cfg()
    yml = build_mediamtx_config(cfg, env)
    for line in ('api: no', 'metrics: no', 'pprof: no', 'playback: no',
                 'rtmp: no', 'hls: no', 'srt: no', 'moq: no'):
        assert line in yml
    assert 'webrtc: yes' in yml
    assert 'webrtcAddress: :8889' in yml


def test_mediamtx_config_enumerates_exactly_the_tier_paths():
    cfg, env = _cfg()
    yml = build_mediamtx_config(cfg, env)
    for path in ('full_t0', 'full_t1', 'top_t0', 'top_t1',
                 'bottom_t0', 'bottom_t1'):
        assert f'  {path}: {{}}' in yml
    # No catch-all path: unknown publish/read attempts must be rejected.
    assert 'all_others' not in yml


def test_mediamtx_config_port_and_hosts_overrides():
    cfg, env = _cfg(WHEP_PORT='9889', WEBRTC_UDP_PORT='9189',
                    WEBRTC_ADDITIONAL_HOSTS='10.1.2.3, stream.example')
    yml = build_mediamtx_config(cfg, env)
    assert 'webrtcAddress: :9889' in yml
    assert 'webrtcLocalUDPAddress: :9189' in yml
    assert 'webrtcAdditionalHosts: [10.1.2.3, stream.example]' in yml
