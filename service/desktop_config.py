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

  CASTER_HOST       Empty selects host mode (xrandr-based auto-detection).
                    Non-empty selects caster mode (no X server present).

  DISPLAY           X11 display, used in host mode only.

  STREAM_WIDTH      Capture width.  Host mode: empty = native screen width.
  STREAM_HEIGHT     Capture height. Host mode: empty = native screen height.
                    Caster mode: empty falls back to 1920x1080.

  DESKTOP_SPLITS    Semicolon-separated 'WxH+X+Y' regions.  Empty in host
                    mode triggers xrandr --listmonitors auto-detection;
                    empty in caster mode falls back to a CROP_HEIGHT-based
                    top/bottom split for backward compatibility.

  CROP_HEIGHT       Caster-mode legacy split point (default = STREAM_HEIGHT
                    so the result is one full-frame split if unset).

  SIGNALLING_PORT   Base port for browser-facing signalling servers.
                    Screen i uses SIGNALLING_PORT + 1 + i.

The computed config is written to /run/desktop-stream/config.json by the
entrypoint and read by the other processes via load_config().
"""
import json
import os
import re
import subprocess
import sys

CONFIG_PATH_DEFAULT = '/run/desktop-stream/config.json'

_GEOM_RE = re.compile(r'^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$')

# xrandr --listmonitors line:
#   " 0: +*HDMI-1 1920/527x1080/297+0+0  HDMI-1"
# We only care about the WxH+X+Y portion (the "/527" / "/297" are physical
# size in mm, which we discard).
_LISTMON_RE = re.compile(
    r'^\s*\d+:\s+\+?\*?\S+\s+(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\b',
)


def _parse_region(s):
    m = _GEOM_RE.match(s.strip())
    if not m:
        raise ValueError(f'invalid region {s!r}: expected WxH+X+Y')
    w, h, x, y = (int(g) for g in m.groups())
    if w <= 0 or h <= 0:
        raise ValueError(f'invalid region {s!r}: width and height must be positive')
    return {'x': x, 'y': y, 'width': w, 'height': h}


def _query_xrandr_screen(display):
    """Return (width, height) of the X server's root window via xrandr."""
    out = subprocess.run(
        ['xrandr', '--display', display, '--current'],
        check=True, timeout=5,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ).stdout.decode(errors='replace')
    # The "Screen 0: minimum 8 x 8, current 3840 x 1080, maximum ..." line.
    m = re.search(r'current\s+(\d+)\s*x\s*(\d+)', out)
    if not m:
        raise RuntimeError(
            f'xrandr did not report a current screen size:\n{out}'
        )
    return int(m.group(1)), int(m.group(2))


def _query_xrandr_monitors(display):
    """Return a list of monitor regions ({'x','y','width','height'}) via xrandr.

    Returns an empty list (and logs a warning) if xrandr is missing or the
    invocation fails — the caller falls back to other strategies.
    """
    try:
        out = subprocess.run(
            ['xrandr', '--display', display, '--listmonitors'],
            check=True, timeout=5,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode(errors='replace')
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired) as exc:
        print(f'[desktop_config] WARNING: xrandr --listmonitors failed '
              f'({exc}); cannot auto-detect monitor geometry.',
              file=sys.stderr)
        return []
    regions = []
    for line in out.splitlines():
        m = _LISTMON_RE.match(line)
        if not m:
            continue
        w, h, x, y = (int(g) for g in m.groups())
        regions.append({'x': x, 'y': y, 'width': w, 'height': h})
    return regions


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
        regions = _query_xrandr_monitors(display)
        if len(regions) >= 2:
            return regions
        # xrandr returned 0 or 1 monitors — fall back to the legacy CROP_HEIGHT
        # split if set, otherwise use the single monitor xrandr reported (or a
        # whole-frame screen if it reported none).
        legacy = _crop_height_split(env, frame_w, frame_h)
        if legacy is not None:
            return legacy
        if regions:
            return regions
        print('[desktop_config] WARNING: xrandr --listmonitors returned no '
              'monitors and CROP_HEIGHT is unset; falling back to a single '
              'full-frame screen.', file=sys.stderr)
        return [{'x': 0, 'y': 0, 'width': frame_w, 'height': frame_h}]

    # Caster mode: there is no X server to query.  Use CROP_HEIGHT if set,
    # otherwise serve a single full-frame screen.
    legacy = _crop_height_split(env, frame_w, frame_h)
    if legacy is not None:
        return legacy
    return [{'x': 0, 'y': 0, 'width': frame_w, 'height': frame_h}]


def compute_config(env=None):
    """Compute the runtime config from environment + xrandr probes.

    Pure with respect to env + xrandr; no side effects on the filesystem.
    """
    env = env if env is not None else os.environ

    host_mode = not bool(env.get('CASTER_HOST', '').strip())
    display   = env.get('DISPLAY', ':0')

    width_raw  = (env.get('STREAM_WIDTH')  or '').strip()
    height_raw = (env.get('STREAM_HEIGHT') or '').strip()

    if width_raw and height_raw:
        width, height = int(width_raw), int(height_raw)
    elif host_mode:
        try:
            native_w, native_h = _query_xrandr_screen(display)
        except (FileNotFoundError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, RuntimeError) as exc:
            print(f'[desktop_config] WARNING: xrandr screen query failed '
                  f'({exc}); falling back to 1920x1080.',
                  file=sys.stderr)
            native_w, native_h = 1920, 1080
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
