"""
x11.py -- X server geometry introspection via python-xlib.

Isolated from desktop_config.py so the pure config math (tier ladders,
crops, region naming) stays testable without an X server or python-xlib
installed.  Xlib is imported lazily: hosts that pin STREAM_WIDTH/HEIGHT and
DESKTOP_SPLITS never touch it.

All failures degrade to None/[] with a warning on stderr — callers choose
their own fallback strategy.
"""
import sys


def _open_xdisplay(display_name):
    """Return an open Xlib.display.Display, or None if Xlib/X server unavailable."""
    try:
        from Xlib import display as xdisplay  # lazy import is intentional
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


def query_x_screen(display_name):
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


def query_x_monitors(display_name):
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
