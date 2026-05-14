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

  DISPLAY           X11 display.

  STREAM_WIDTH      Capture width.  Empty = native screen width via xrandr.
  STREAM_HEIGHT     Capture height. Empty = native screen height via xrandr.

  STREAM_FRAMERATE  Target frames-per-second.  Drives the videorate caps
                    filter and surfaced in /config.json so the browser
                    gumball can gate its top quality tiers on the delivered
                    fps matching the target.  Default: 30.

  DESKTOP_SPLITS    Semicolon-separated 'WxH+X+Y' regions.  Empty triggers
                    RandR auto-detection of the connected monitors.

  SIGNALLING_PORT   Base port for browser-facing signalling servers.
                    Screen i uses SIGNALLING_PORT + 1 + i for its scale-1.0
                    tier; subsequent tiers add SIGNALLING_PORT_STRIDE per
                    tier index.

  WEBRTC_SCALE_LADDER  Comma-separated fractional scales for the per-stream
                       webrtcsink ladder.  Each browser auto-picks the
                       smallest tier whose dimensions still meet its
                       rendered video size, so an idle small-window viewer
                       pulls a 1/4-pixel-count stream instead of forcing
                       the encoder to deliver full-source frames it would
                       just downscale on the client.

                       Accepts decimals (``"0.5"``), ints (``"1"``) and
                       ratios (``"1/3"``).  Values must be in (0, 1.0]; the
                       ``1.0`` tier is always included.  Default:
                       ``"1.0,0.75,0.5,0.25"``.

  SIGNALLING_PORT_STRIDE  Spacing between successive tier ports per stream.
                          Default 100 (full=8443/8543/8643/8743, ...).

The computed config is written to /run/desktop-stream/config.json by the
entrypoint and read by the other processes via load_config().
"""
import json
import os
import re
import sys

CONFIG_PATH_DEFAULT = '/run/desktop-stream/config.json'

_GEOM_RE = re.compile(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$')

# Defaults for the WebRTC tier ladder (see _parse_scale_ladder).
_DEFAULT_SCALE_LADDER = '1.0,0.75,0.5,0.25'
# Tiers smaller than this in either dimension are skipped (except the 1.0
# tier, which is always kept).  WebRTC video below ~64px is rarely useful and
# small encoded sizes hit codec floors anyway.
_TIER_MIN_DIM       = 64
# Hard cap on tier count so a misconfigured ladder can't blow up port
# allocation or pipeline element count.
_TIER_MAX_COUNT     = 8
# Dedup tolerance — two scales that differ by less than this are the same.
_SCALE_EPS          = 1e-6


def _parse_scale_ladder(env):
    """Parse the comma-separated WEBRTC_SCALE_LADDER env var.

    Returns a list of unique floats sorted descending, always including
    ``1.0``.  Each entry must be in (0, 1.0]; we reject values outside that
    range explicitly rather than clamping so an operator typo (e.g.
    ``"1,5"`` meaning ``[1.0, 5.0]``) surfaces as an error instead of being
    silently saturated.
    """
    raw = (env.get('WEBRTC_SCALE_LADDER') or _DEFAULT_SCALE_LADDER).strip()
    if not raw:
        raw = _DEFAULT_SCALE_LADDER
    out = []
    for piece in raw.split(','):
        piece = piece.strip()
        if not piece:
            continue
        try:
            if '/' in piece:
                num, _, den = piece.partition('/')
                value = float(num) / float(den)
            else:
                value = float(piece)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(
                f'WEBRTC_SCALE_LADDER entry {piece!r} is not a number or '
                f'ratio: {exc}'
            ) from exc
        if not (0 < value <= 1.0):
            raise ValueError(
                f'WEBRTC_SCALE_LADDER entry {piece!r} = {value} is outside '
                f'(0, 1.0]'
            )
        out.append(value)

    # Ensure 1.0 is always present — every stream needs a passthrough tier
    # so the legacy fullSignallingPort / screen.signallingPort fields below
    # remain meaningful.
    if not any(abs(v - 1.0) < _SCALE_EPS for v in out):
        out.append(1.0)

    out.sort(reverse=True)  # largest scale first; tier 0 == 1.0
    dedup = []
    for v in out:
        if not any(abs(v - prev) < _SCALE_EPS for prev in dedup):
            dedup.append(v)

    if len(dedup) > _TIER_MAX_COUNT:
        raise ValueError(
            f'WEBRTC_SCALE_LADDER has {len(dedup)} entries; max is '
            f'{_TIER_MAX_COUNT}'
        )
    return dedup


def _compute_tiers(base_w, base_h, scales, min_dim=_TIER_MIN_DIM):
    """Apply ``scales`` to (base_w, base_h), snapping each result to even
    integers and dropping any tier that falls below ``min_dim`` in either
    dimension (the 1.0 tier is always retained).

    Returns a list of ``{'scale', 'width', 'height'}`` dicts in the same
    order as ``scales``, after a second dedup pass that catches the case
    where rounding collapses two distinct scales onto the same pixel size
    (e.g. base=4, scale=0.5 → 2; scale=0.49 → 2).
    """
    tiers = []
    seen_dims = set()
    for scale in scales:
        # round-half-to-even into pixels, then snap to nearest even.  We
        # need even dims because videoscale's NV12/I420 paths refuse odd
        # widths/heights (YUV 4:2:0 sub-sampling).
        w = max(2, 2 * round(base_w * scale / 2))
        h = max(2, 2 * round(base_h * scale / 2))
        is_passthrough = abs(scale - 1.0) < _SCALE_EPS
        if not is_passthrough and (w < min_dim or h < min_dim):
            continue
        if (w, h) in seen_dims and not is_passthrough:
            continue
        seen_dims.add((w, h))
        tiers.append({'scale': scale, 'width': w, 'height': h})
    return tiers


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


def _crops_from_region(region, frame_w, frame_h):
    """Compute videocrop edge trims that select *region* out of frame_w x frame_h."""
    return {
        'left':   max(0, region['x']),
        'top':    max(0, region['y']),
        'right':  max(0, frame_w - region['x'] - region['width']),
        'bottom': max(0, frame_h - region['y'] - region['height']),
    }


def _splits_from_env(env, frame_w, frame_h, display):
    """Return list of {'region': ..., 'crop': ...} dicts for the configured splits."""
    raw = (env.get('DESKTOP_SPLITS') or '').strip()
    if raw:
        regions = [_parse_region(s) for s in raw.split(';') if s.strip()]
        return [{'region': r, 'crop': _crops_from_region(r, frame_w, frame_h)}
                for r in regions]

    regions = _query_x_monitors(display)
    if regions:
        return [{'region': r, 'crop': _crops_from_region(r, frame_w, frame_h)}
                for r in regions]

    print('[desktop_config] WARNING: no monitor geometry available; '
          'falling back to a single full-frame screen.', file=sys.stderr)
    full = {'x': 0, 'y': 0, 'width': frame_w, 'height': frame_h}
    return [{'region': full, 'crop': {'left': 0, 'top': 0, 'right': 0, 'bottom': 0}}]


def compute_config(env=None):
    """Compute the runtime config from environment + RandR probes.

    Pure with respect to env + Xlib; no side effects on the filesystem.
    """
    env = env if env is not None else os.environ

    display   = env.get('DISPLAY', ':0')

    width_raw  = (env.get('STREAM_WIDTH')  or '').strip()
    height_raw = (env.get('STREAM_HEIGHT') or '').strip()

    if width_raw and height_raw:
        width, height = int(width_raw), int(height_raw)
    else:
        native = _query_x_screen(display)
        if native is None:
            print('[desktop_config] WARNING: X screen size unavailable; '
                  'falling back to 1920x1080.', file=sys.stderr)
            native = (1920, 1080)
        native_w, native_h = native
        width  = int(width_raw)  if width_raw  else native_w
        height = int(height_raw) if height_raw else native_h

    sig_port    = int(env.get('SIGNALLING_PORT',         '8443'))
    sig_stride  = int(env.get('SIGNALLING_PORT_STRIDE',  '100'))
    framerate   = int((env.get('STREAM_FRAMERATE')   or  '30').strip())
    scales      = _parse_scale_ladder(env)

    splits = _splits_from_env(env, width, height, display)
    named = _name_regions([s['region'] for s in splits])

    # Pair each named region back with its crop info.  _name_regions may
    # reorder the regions, so look them up by identity from the original list.
    region_to_crop = {id(s['region']): s['crop'] for s in splits}

    # Stream index 0 is the full-frame; screens are 1..N.  Each (stream,
    # tier) pair uses port = SIGNALLING_PORT + stream_idx + stride * tier_idx.
    # The stride defaults to 100, leaving room for up to ~99 screens before
    # tier 1's allocation would collide with tier 0 of another stream.  We
    # assert that explicitly so a misconfigured deployment fails loudly
    # instead of silently double-binding a port.
    if len(named) + 1 >= sig_stride:
        raise ValueError(
            f'SIGNALLING_PORT_STRIDE={sig_stride} is too small for '
            f'{len(named) + 1} streams (full + {len(named)} screens)'
        )

    def _port_for(stream_idx, tier_idx):
        return sig_port + stream_idx + sig_stride * tier_idx

    def _tier_list_with_ports(tiers, stream_idx):
        return [
            {
                'scale':          t['scale'],
                'width':          t['width'],
                'height':         t['height'],
                'signallingPort': _port_for(stream_idx, tier_idx),
            }
            for tier_idx, t in enumerate(tiers)
        ]

    full_tiers = _tier_list_with_ports(
        _compute_tiers(width, height, scales), stream_idx=0)

    screens = []
    for i, (region, name) in enumerate(named):
        crop = region_to_crop[id(region)]
        stream_idx = i + 1
        screen_tiers = _tier_list_with_ports(
            _compute_tiers(region['width'], region['height'], scales),
            stream_idx=stream_idx,
        )
        screens.append({
            'name':            name,
            'path':            f'/{name}',
            # signallingPort is the scale=1.0 (tier 0) port — kept for
            # legacy callers that haven't been taught about the tiers list.
            'signallingPort':  screen_tiers[0]['signallingPort'],
            'x':               region['x'],
            'y':               region['y'],
            'width':           region['width'],
            'height':          region['height'],
            'cropLeft':        crop['left'],
            'cropTop':         crop['top'],
            'cropRight':       crop['right'],
            'cropBottom':      crop['bottom'],
            'tiers':           screen_tiers,
        })

    return {
        'desktopName':         env.get('DESKTOP_NAME', 'desktop'),
        'width':               width,
        'height':              height,
        'framerate':           framerate,
        # Kept for backward compatibility — same value as fullTiers[0].
        'fullSignallingPort':  full_tiers[0]['signallingPort'],
        'fullTiers':           full_tiers,
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
    """Read a previously-written config file.

    When the file does not exist (e.g. in unit tests that import the web
    server module without running the entrypoint), fall back to computing
    a fresh config from the current environment.
    """
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return compute_config()


if __name__ == '__main__':
    cfg = write_config()
    json.dump(cfg, sys.stdout, indent=2)
    sys.stdout.write('\n')
