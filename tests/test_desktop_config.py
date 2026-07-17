"""
Unit tests for desktop_config.compute_config().

The X probes (x11.py, python-xlib) are monkey-patched, so no X server —
and no Docker — is required.
"""

import pytest

import desktop_config


@pytest.fixture(autouse=True)
def _stub_x_probes(monkeypatch):
    """By default no X probes succeed; tests opt-in case-by-case."""
    monkeypatch.setattr(desktop_config, '_query_x_screen',
                        lambda _display: None)
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _display: [])


def test_explicit_desktop_splits_horizontal_pair():
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '3840',
        'STREAM_HEIGHT': '1080',
        'DESKTOP_SPLITS': '1920x1080+0+0;1920x1080+1920+0',
    })
    names = [s['name'] for s in cfg['screens']]
    assert names == ['left', 'right']
    left, right = cfg['screens']
    assert (left['x'], left['y'], left['width'], left['height']) \
        == (0, 0, 1920, 1080)
    assert (right['x'], right['y'], right['width'], right['height']) \
        == (1920, 0, 1920, 1080)


def test_explicit_desktop_splits_three_screens_use_screen_n():
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '3840',
        'STREAM_HEIGHT': '2160',
        # Out of reading order on purpose — code must sort.
        'DESKTOP_SPLITS': '1920x1080+1920+1080;1920x1080+0+0;1920x1080+1920+0',
    })
    # Reading order: top row left-to-right, then bottom row.
    names = [(s['name'], s['x'], s['y']) for s in cfg['screens']]
    assert names == [
        ('screen1', 0,    0),
        ('screen2', 1920, 0),
        ('screen3', 1920, 1080),
    ]


def test_native_resolution_from_x_server(monkeypatch):
    monkeypatch.setattr(desktop_config, '_query_x_screen',
                        lambda _d: (3840, 1080))
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _d: [
                            {'x': 0,    'y': 0, 'width': 1920, 'height': 1080},
                            {'x': 1920, 'y': 0, 'width': 1920, 'height': 1080},
                        ])
    cfg = desktop_config.compute_config({})
    assert cfg['width'] == 3840
    assert cfg['height'] == 1080
    assert [s['name'] for s in cfg['screens']] == ['left', 'right']


def test_explicit_dimensions_skip_screen_query(monkeypatch):
    # _query_x_screen must not be called when both dims are given.
    def _fail(_d):
        raise AssertionError('_query_x_screen should not be called')
    monkeypatch.setattr(desktop_config, '_query_x_screen', _fail)
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _d: [{'x': 0, 'y': 0, 'width': 1280, 'height': 720}])
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '1280',
        'STREAM_HEIGHT': '720',
    })
    assert (cfg['width'], cfg['height']) == (1280, 720)


def test_falls_back_to_full_frame_when_x_quiet():
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '1280',
        'STREAM_HEIGHT': '720',
    })
    # No DESKTOP_SPLITS, no monitors from RandR → single full-frame screen.
    assert [s['name'] for s in cfg['screens']] == ['screen1']
    s = cfg['screens'][0]
    assert (s['x'], s['y'], s['width'], s['height']) == (0, 0, 1280, 720)


def test_whep_paths_use_stream_key_and_tier_index():
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '3840',
        'STREAM_HEIGHT': '1080',
        'DESKTOP_SPLITS': '1920x1080+0+0;1920x1080+1920+0',
        'LIVE_SCALE_LADDER': '1.0',
    })
    assert [t['whepPath'] for t in cfg['fullTiers']] == ['full_t0']
    assert [t['whepPath'] for t in cfg['screens'][0]['tiers']] == ['left_t0']
    assert [t['whepPath'] for t in cfg['screens'][1]['tiers']] == ['right_t0']


def test_whep_port_default_and_override():
    assert desktop_config.compute_config({
        'STREAM_WIDTH': '1280', 'STREAM_HEIGHT': '720',
    })['webrtcPort'] == 8889
    assert desktop_config.compute_config({
        'STREAM_WIDTH': '1280', 'STREAM_HEIGHT': '720',
        'WHEP_PORT': '9889',
    })['webrtcPort'] == 9889


def test_randr_regions_scale_to_downscaled_frame(monkeypatch):
    # Native 3840x1080 (two side-by-side monitors), capture downscaled to
    # 1920x540: regions scale by the same ratio so crops select the right
    # pixels in the scaled frame.
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _d: [
                            {'x': 0,    'y': 0, 'width': 1920, 'height': 1080},
                            {'x': 1920, 'y': 0, 'width': 1920, 'height': 1080},
                        ])
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '1920', 'STREAM_HEIGHT': '540',
    })
    left, right = cfg['screens']
    assert (left['x'], left['y'], left['width'], left['height']) \
        == (0, 0, 960, 540)
    assert (right['x'], right['y'], right['width'], right['height']) \
        == (960, 0, 960, 540)


def test_randr_region_scaling_keeps_seams_and_even_dims(monkeypatch):
    # Awkward ratio (1/3 width, 2/3 height): adjacent monitors must still
    # share their seam exactly, and every region dimension stays even.
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _d: [
                            {'x': 0,    'y': 0, 'width': 1920, 'height': 1080},
                            {'x': 1920, 'y': 0, 'width': 1920, 'height': 1080},
                        ])
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '1280', 'STREAM_HEIGHT': '720',
    })
    left, right = cfg['screens']
    assert left['x'] + left['width'] == right['x']          # shared seam
    assert right['x'] + right['width'] == 1280              # spans the frame
    for s in (left, right):
        assert s['width'] % 2 == 0 and s['height'] % 2 == 0
        assert s['height'] == 720


def test_out_of_frame_region_warns(capsys):
    # Regions are used verbatim by the ffmpeg crop, so one that exceeds the
    # capture frame is a misconfiguration — flagged loudly, not clamped.
    desktop_config.compute_config({
        'STREAM_WIDTH': '1920', 'STREAM_HEIGHT': '1080',
        'DESKTOP_SPLITS': '1920x1080+0+0;1920x1080+1920+0',
    })
    assert 'does not fit the 1920x1080 capture frame' in capsys.readouterr().err


def test_invalid_desktop_splits_raises():
    with pytest.raises(ValueError):
        desktop_config.compute_config({
            'DESKTOP_SPLITS': 'not-a-region',
        })


def test_desktop_name_propagates():
    cfg = desktop_config.compute_config({'DESKTOP_NAME': 'workstation-a'})
    assert cfg['desktopName'] == 'workstation-a'


class TestScaleLadder:
    """Cover _parse_scale_ladder + _compute_tiers + tier emission in
    compute_config.  These all exercise the new ladder feature end-to-end
    without standing up a real pipeline."""

    def test_default_ladder(self):
        # Two tiers by default: unlike the old per-consumer webrtcsink
        # encoders, every ladder entry is an always-on ffmpeg encode per
        # stream, so the default ladder stays small.
        assert desktop_config._parse_scale_ladder({}) == [1.0, 0.5]

    def test_explicit_ladder_sorts_descending(self):
        assert desktop_config._parse_scale_ladder(
            {'LIVE_SCALE_LADDER': '0.5,1.0,0.25'}
        ) == [1.0, 0.5, 0.25]

    def test_ratio_form_parses(self):
        # 1/3 ≈ 0.333; we accept it as a tier scale.
        out = desktop_config._parse_scale_ladder(
            {'LIVE_SCALE_LADDER': '1.0,1/3'}
        )
        assert out[0] == 1.0
        assert abs(out[1] - 1/3) < 1e-9

    def test_dedup_collapses_near_duplicates(self):
        # Dedup uses a small float tolerance, so 0.5 and 0.50000001 are
        # treated as the same tier.  Whichever appears first after the
        # descending sort wins — we don't care which; we only care that
        # the final list has exactly the two distinct tiers.
        out = desktop_config._parse_scale_ladder(
            {'LIVE_SCALE_LADDER': '1.0,0.5,0.5,0.50000001'}
        )
        assert len(out) == 2
        assert out[0] == 1.0
        assert abs(out[1] - 0.5) < 1e-6

    def test_one_is_always_included(self):
        # Operator forgot to include 1.0 — we silently add it so every
        # stream has a passthrough tier.
        assert desktop_config._parse_scale_ladder(
            {'LIVE_SCALE_LADDER': '0.5,0.25'}
        ) == [1.0, 0.5, 0.25]

    @pytest.mark.parametrize('bad', ['0', '-0.5', '1.5', '2', 'abc', '1/0'])
    def test_rejects_invalid_scales(self, bad):
        with pytest.raises(ValueError):
            desktop_config._parse_scale_ladder({'LIVE_SCALE_LADDER': bad})

    def test_rejects_too_many_tiers(self):
        ladder = ','.join(str(round(1 - i * 0.05, 3)) for i in range(20))
        with pytest.raises(ValueError):
            desktop_config._parse_scale_ladder(
                {'LIVE_SCALE_LADDER': ladder})

    def test_compute_tiers_snaps_to_even(self):
        # Banker's rounding: 1.0×1281/2 = 640.5 → 640, ×2 = 1280.  Same for
        # 721 → 720.  Down-snap is the right direction so we never ask
        # videoscale to produce more pixels than the source can supply.
        tiers = desktop_config._compute_tiers(1281, 721, [1.0, 0.5, 0.25])
        assert tiers == [
            {'scale': 1.0,  'width': 1280, 'height': 720},
            {'scale': 0.5,  'width': 640,  'height': 360},
            {'scale': 0.25, 'width': 320,  'height': 180},
        ]

    def test_compute_tiers_drops_below_min_dim(self):
        # 1.0 tier always kept; 0.1 tier of 200x200 = 20x20 < 64 → dropped.
        tiers = desktop_config._compute_tiers(
            200, 200, [1.0, 0.5, 0.25, 0.1], min_dim=64)
        scales = [t['scale'] for t in tiers]
        assert 1.0 in scales
        assert 0.1 not in scales

    def test_compute_tiers_dedups_after_rounding(self):
        # base=4 px: every non-trivial scale rounds to 2.  Only the 1.0
        # tier is retained.
        tiers = desktop_config._compute_tiers(4, 4, [1.0, 0.5, 0.49],
                                              min_dim=2)
        # Even-dim snapping makes 1.0 → 4x4, 0.5 and 0.49 both → 2x2;
        # passthrough kept, the rest dedup.
        assert tiers == [
            {'scale': 1.0,  'width': 4, 'height': 4},
            {'scale': 0.5,  'width': 2, 'height': 2},
        ]

    def test_full_and_screen_tiers_emitted(self):
        cfg = desktop_config.compute_config({
            'STREAM_WIDTH': '1920',
            'STREAM_HEIGHT': '1080',
            'DESKTOP_SPLITS': '1920x540+0+0;1920x540+0+540',
            'LIVE_SCALE_LADDER': '1.0,0.5',
        })
        # Full stream tiers.
        assert cfg['fullTiers'] == [
            {'scale': 1.0, 'width': 1920, 'height': 1080,
             'whepPath': 'full_t0'},
            {'scale': 0.5, 'width': 960,  'height': 540,
             'whepPath': 'full_t1'},
        ]
        # Per-screen tiers: each cropped region is 1920x540.
        top, bottom = cfg['screens']
        assert top['tiers'] == [
            {'scale': 1.0, 'width': 1920, 'height': 540,
             'whepPath': 'top_t0'},
            {'scale': 0.5, 'width': 960,  'height': 270,
             'whepPath': 'top_t1'},
        ]
        assert bottom['tiers'] == [
            {'scale': 1.0, 'width': 1920, 'height': 540,
             'whepPath': 'bottom_t0'},
            {'scale': 0.5, 'width': 960,  'height': 270,
             'whepPath': 'bottom_t1'},
        ]

    def test_whep_paths_unique_across_streams_and_tiers(self):
        cfg = desktop_config.compute_config({
            'STREAM_WIDTH': '3840',
            'STREAM_HEIGHT': '1080',
            'DESKTOP_SPLITS': '1920x1080+0+0;1920x1080+1920+0',
            'LIVE_SCALE_LADDER': '1.0,0.5,0.25',
        })
        paths = [t['whepPath'] for t in cfg['fullTiers']]
        for s in cfg['screens']:
            paths += [t['whepPath'] for t in s['tiers']]
        assert len(paths) == len(set(paths))
        assert paths[0] == 'full_t0'
