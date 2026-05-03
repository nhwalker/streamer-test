#!/usr/bin/env python3
"""
desktop_config.py -- single source of truth for desktop name, capture
resolution, and per-screen splits.

The container's three Python processes (pipeline.py, web_server.py, and the
entrypoint helper) each load the same config so they agree on:

  * The desktop name (page header + archive filename prefix).
  * The capture width x height.
  * The list of named screen regions, each with its WebRTC signalling port.

Inputs (environment variables, evaluated once at container start):

  DESKTOP_NAME      Page-header label and archive filename prefix.
                    Default: 'desktop'.

  CASTER_HOST       Empty selects host mode (X RandR auto-detection via
                    python-xlib).  Non-empty selects caster mode.

  DISPLAY           X11 display, used in host mode only.

  STREAM_WIDTH      Capture width.  Host mode: empty = native screen width.
  STREAM_HEIGHT     Capture height. Host mode: empty = native screen height.
                    Caster mode: empty falls back to 1920x1080.

  DESKTOP_SPLITS    Semicolon-separated 'WxH+X+Y' regions.  Empty in host
                    mode triggers RandR auto-detection; empty in caster
                    mode falls back to a CROP_HEIGHT-based top/bottom
                    split for backward compatibility.

  CROP_HEIGHT       Caster-mode legacy split point.  Also used in host
                    mode when RandR returns fewer than 2 monitors.

  SIGNALLING_PORT   Base port for browser-facing signalling servers.
                    Screen i uses SIGNALLING_PORT + 1 + i.

The computed config is written to /run/desktop-stream/config.json by the
entrypoint and read by the other processes via load_config().
"""
import json
import os
import re
import sys

CONFIG_PATH_DEFAULT = '/run/desktop-stream/config.json'

_GEOM_RE = re.compile(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$')


def _parse_region(s):
    m = _GEOM_RE.match(s.strip())
    if not m:
        raise ValueError(f'invalid region {s!r}: expected WxH+X+Y')
    w, h, x, y = (int(g) for g in m.groups())
    if w <= 0 or h <= 0:
        raise ValueError(f'invalid region {s!r}: width and height must be positive')
    return {'x': x, 'y': y, 'width': w, 'height': h}


def _open_xdisplay(display_name):
    """Return an open Xlib.display.Display, or None if Xlib/X server unavailable."""
    try:
        from Xlib import display as xdisplay  # noqa: WPS433 - lazy import is intentional
    except ImportError as exc:
        print(f'[desktop_config] WARNING: python-xlib unavailable ({exc}); '
              'cannot auto-detect screen geometry.', file=sys.stderr)
        return None
    try:
        return xdisplay.Display(display_name)
    except Exception as exc:  # DisplayConnectionError, DisplayNameError, ...
        print(f'[desktop_config] WARNING: cannot open X display {display_name!r} '
              f'({exc}); cannot auto-detect screen geometry.', file=sys.stderr)
        return None


def _query_x_screen(display_name):
    """Return (width, height) of the X server's root window, or None on failure."""
    d = _open_xdisplay(display_name)
    if d is None:
        return None
    try:
        s = d.screen()
        return s.width_in_pixels, s.height_in_pixels
    except Exception as exc:
        print(f'[desktop_config] WARNING: X screen query failed ({exc}); '
              'cannot auto-detect screen size.', file=sys.stderr)
        return None
    finally:
        try:
            d.close()
        except Exception:
            pass


def _query_x_monitors(display_name):
    """Return a list of monitor regions ({'x','y','width','height'}) via RandR.

    Tries XRRGetMonitors (RandR 1.5) first; falls back to enumerating active
    CRTCs.  Returns [] when no usable data is available — the caller chooses
    its own fallback strategy.
    """
    d = _open_xdisplay(display_name)
    if d is None:
        return []
    try:
        root = d.screen().root
        # RandR 1.5: one entry per logical monitor (handles cloned outputs).
        try:
            res = root.xrandr_get_monitors()
            regions = [
                {'x': m.x, 'y': m.y,
                 'width': m.width_in_pixels, 'height': m.height_in_pixels}
                for m in res.monitors
                if m.width_in_pixels > 0 and m.height_in_pixels > 0
            ]
            if regions:
                return regions
        except Exception as exc:
            print(f'[desktop_config] xrandr_get_monitors failed ({exc}); '
                  'trying CRTC enumeration.', file=sys.stderr)

        # Fallback: enumerate active CRTCs from the screen resources.
        try:
            res = root.xrandr_get_screen_resources_current()
        except Exception as exc:
            print(f'[desktop_config] xrandr_get_screen_resources_current '
                  f'failed ({exc}); cannot enumerate monitors.',
                  file=sys.stderr)
            return []

        regions = []
        for crtc_id in res.crtcs:
            try:
                info = d.xrandr_get_crtc_info(crtc_id, res.config_timestamp)
            except Exception:
                continue
            if info.width > 0 and info.height > 0:
                regions.append({
                    'x': info.x, 'y': info.y,
                    'width': info.width, 'height': info.height,
                })
        return regions
    finally:
        try:
            d.close()
        except Exception:
            pass


def _name_regions(regions):
    """Assign a name to each region per the documented rules.

    * Exactly 2 regions whose x ranges do not overlap -> 'left' / 'right'.
    * Exactly 2 regions whose y ranges do not overlap -> 'top'  / 'bottom'.
    * Otherwise -> 'screen1', 'screen2', ... ordered by (y center, x center).
    """
    if len(regions) == 2:
        a, b = regions
        ax2 = a['x'] + a['width']
        bx2 = b['x'] + b['width']
        ay2 = a['y'] + a['height']
        by2 = b['y'] + b['height']
        if ax2 <= b['x'] or bx2 <= a['x']:
            left, right = (a, b) if a['x'] < b['x'] else (b, a)
            return [(left, 'left'), (right, 'right')]
        if ay2 <= b['y'] or by2 <= a['y']:
            top, bottom = (a, b) if a['y'] < b['y'] else (b, a)
            return [(top, 'top'), (bottom, 'bottom')]

    ordered = sorted(
        regions,
        key=lambda r: (r['y'] + r['height'] / 2.0,
                       r['x'] + r['width'] / 2.0),
    )
    return [(r, f'screen{i + 1}') for i, r in enumerate(ordered)]


def _crop_height_split(env, frame_w, frame_h):
    """Return the legacy CROP_HEIGHT split, or None if it isn't usable."""
    crop_raw = env.get('CROP_HEIGHT')
    if not crop_raw:
        return None
    try:
        crop = int(crop_raw)
    except ValueError:
        return None
    if crop <= 0 or crop >= frame_h:
        return None
    return [
        {'x': 0, 'y': 0,    'width': frame_w, 'height': crop},
        {'x': 0, 'y': crop, 'width': frame_w, 'height': frame_h - crop},
    ]


def _splits_from_env(env, host_mode, frame_w, frame_h, display):
    """Return the unnamed list of regions implied by DESKTOP_SPLITS+context."""
    raw = (env.get('DESKTOP_SPLITS') or '').strip()
    if raw:
        return [_parse_region(s) for s in raw.split(';') if s.strip()]

    if host_mode:
        regions = _query_x_monitors(display)
        if len(regions) >= 2:
            return regions
        # 0-1 monitors from RandR — fall back to legacy CROP_HEIGHT if set,
        # otherwise use the single monitor (or whole frame).
        legacy = _crop_height_split(env, frame_w, frame_h)
        if legacy is not None:
            return legacy
        if regions:
            return regions
        print('[desktop_config] WARNING: no monitor geometry available and '
              'CROP_HEIGHT is unset; falling back to a single full-frame '
              'screen.', file=sys.stderr)
        return [{'x': 0, 'y': 0, 'width': frame_w, 'height': frame_h}]

    # Caster mode: there is no X server to query.  Use CROP_HEIGHT if set,
    # otherwise serve a single full-frame screen.
    legacy = _crop_height_split(env, frame_w, frame_h)
    if legacy is not None:
        return legacy
    return [{'x': 0, 'y': 0, 'width': frame_w, 'height': frame_h}]


def compute_config(env=None):
    """Compute the runtime config from environment + RandR probes.

    Pure with respect to env + Xlib; no side effects on the filesystem.
    """
    env = env if env is not None else os.environ

    host_mode = not bool(env.get('CASTER_HOST', '').strip())
    display   = env.get('DISPLAY', ':0')

    width_raw  = (env.get('STREAM_WIDTH')  or '').strip()
    height_raw = (env.get('STREAM_HEIGHT') or '').strip()

    if width_raw and height_raw:
        width, height = int(width_raw), int(height_raw)
    elif host_mode:
        native = _query_x_screen(display)
        if native is None:
            print('[desktop_config] WARNING: X screen size unavailable; '
                  'falling back to 1920x1080.', file=sys.stderr)
            native = (1920, 1080)
        native_w, native_h = native
        width  = int(width_raw)  if width_raw  else native_w
        height = int(height_raw) if height_raw else native_h
    else:
        width  = int(width_raw)  if width_raw  else 1920
        height = int(height_raw) if height_raw else 1080

    sig_port = int(env.get('SIGNALLING_PORT', '8443'))

    raw_regions = _splits_from_env(env, host_mode, width, height, display)
    named = _name_regions(raw_regions)

    screens = []
    for i, (region, name) in enumerate(named):
        screens.append({
            'name':            name,
            'path':            f'/{name}',
            'signallingPort':  sig_port + 1 + i,
            'x':               region['x'],
            'y':               region['y'],
            'width':           region['width'],
            'height':          region['height'],
        })

    return {
        'desktopName':         env.get('DESKTOP_NAME', 'desktop'),
        'mode':                'host' if host_mode else 'caster',
        'width':               width,
        'height':              height,
        'fullSignallingPort':  sig_port,
        'screens':             screens,
    }


def write_config(path=CONFIG_PATH_DEFAULT, env=None):
    """Compute the config and write it to *path* (creating parent dirs)."""
    cfg = compute_config(env)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        json.dump(cfg, fh, indent=2)
    return cfg


def load_config(path=CONFIG_PATH_DEFAULT):
    """Read a previously-written config file."""
    with open(path) as fh:
        return json.load(fh)


if __name__ == '__main__':
    cfg = write_config()
    json.dump(cfg, sys.stdout, indent=2)
    sys.stdout.write('\n')
