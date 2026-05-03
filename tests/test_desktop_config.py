"""
Unit tests for desktop_config.compute_config().

xrandr is invoked through subprocess so we monkey-patch the helpers that
wrap it.  No Docker / GStreamer / X server required.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))
import desktop_config  # noqa: E402


@pytest.fixture(autouse=True)
def _stub_x_probes(monkeypatch):
    """By default no X probes succeed; tests opt-in case-by-case."""
    monkeypatch.setattr(desktop_config, '_query_x_screen',
                        lambda _display: None)
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _display: [])


def test_caster_mode_defaults_to_one_full_screen():
    cfg = desktop_config.compute_config({
        'CASTER_HOST': '127.0.0.1',
    })
    assert cfg['mode'] == 'caster'
    assert cfg['width'] == 1920
    assert cfg['height'] == 1080
    assert cfg['desktopName'] == 'desktop'
    assert cfg['fullSignallingPort'] == 8443
    assert len(cfg['screens']) == 1
    s = cfg['screens'][0]
    assert s['name'] == 'screen1'
    assert s['signallingPort'] == 8444
    assert (s['x'], s['y'], s['width'], s['height']) == (0, 0, 1920, 1080)


def test_caster_mode_crop_height_yields_top_bottom():
    cfg = desktop_config.compute_config({
        'CASTER_HOST': '127.0.0.1',
        'STREAM_WIDTH': '1280',
        'STREAM_HEIGHT': '720',
        'CROP_HEIGHT': '360',
    })
    assert [s['name'] for s in cfg['screens']] == ['top', 'bottom']
    top, bot = cfg['screens']
    assert (top['y'], top['height']) == (0, 360)
    assert (bot['y'], bot['height']) == (360, 360)
    assert top['signallingPort'] == 8444
    assert bot['signallingPort'] == 8445


def test_explicit_desktop_splits_horizontal_pair():
    cfg = desktop_config.compute_config({
        'CASTER_HOST': '127.0.0.1',
        'STREAM_WIDTH': '3840',
        'STREAM_HEIGHT': '1080',
        'DESKTOP_SPLITS': '1920x1080+0+0;1920x1080+1920+0',
    })
    names = [s['name'] for s in cfg['screens']]
    assert names == ['left', 'right']
    left, right = cfg['screens']
    assert left['x'] == 0
    assert right['x'] == 1920


def test_explicit_desktop_splits_three_screens_use_screen_n():
    cfg = desktop_config.compute_config({
        'CASTER_HOST': '127.0.0.1',
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


def test_host_mode_native_resolution_from_x_server(monkeypatch):
    monkeypatch.setattr(desktop_config, '_query_x_screen',
                        lambda _d: (3840, 1080))
    monkeypatch.setattr(desktop_config, '_query_x_monitors',
                        lambda _d: [
                            {'x': 0,    'y': 0, 'width': 1920, 'height': 1080},
                            {'x': 1920, 'y': 0, 'width': 1920, 'height': 1080},
                        ])
    cfg = desktop_config.compute_config({})
    assert cfg['mode'] == 'host'
    assert cfg['width'] == 3840
    assert cfg['height'] == 1080
    assert [s['name'] for s in cfg['screens']] == ['left', 'right']


def test_host_mode_explicit_dimensions_skip_screen_query(monkeypatch):
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


def test_host_mode_falls_back_to_crop_height_when_x_quiet():
    cfg = desktop_config.compute_config({
        'STREAM_WIDTH': '1280',
        'STREAM_HEIGHT': '720',
        'CROP_HEIGHT': '360',
    })
    assert [s['name'] for s in cfg['screens']] == ['top', 'bottom']


def test_signalling_port_offset_uses_index():
    cfg = desktop_config.compute_config({
        'CASTER_HOST': 'h',
        'STREAM_WIDTH': '3840',
        'STREAM_HEIGHT': '1080',
        'SIGNALLING_PORT': '9000',
        'DESKTOP_SPLITS': '1920x1080+0+0;1920x1080+1920+0',
    })
    assert cfg['fullSignallingPort'] == 9000
    assert cfg['screens'][0]['signallingPort'] == 9001
    assert cfg['screens'][1]['signallingPort'] == 9002


def test_invalid_desktop_splits_raises():
    with pytest.raises(ValueError):
        desktop_config.compute_config({
            'CASTER_HOST': 'h',
            'DESKTOP_SPLITS': 'not-a-region',
        })


def test_desktop_name_propagates():
    cfg = desktop_config.compute_config({'DESKTOP_NAME': 'workstation-a'})
    assert cfg['desktopName'] == 'workstation-a'
