"""
Unit tests for pipeline.py's pure helpers: the ARCHIVE_ENABLED flag
parser and the [ffmpeg]-prefixed output forwarding.  Process supervision
itself is exercised by the functional tests.
"""

import io
import subprocess
import sys

import pytest

from pipeline import env_flag, forward_output, spawn_ffmpeg


# ── env_flag ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw', ['0', 'false', 'no', 'off', 'False', ' NO '])
def test_env_flag_falsy_values(raw):
    assert env_flag({'ARCHIVE_ENABLED': raw}, 'ARCHIVE_ENABLED') is False


@pytest.mark.parametrize('raw', ['1', 'true', 'yes', 'on', 'anything'])
def test_env_flag_truthy_values(raw):
    assert env_flag({'ARCHIVE_ENABLED': raw}, 'ARCHIVE_ENABLED') is True


def test_env_flag_unset_or_empty_uses_default():
    assert env_flag({}, 'ARCHIVE_ENABLED') is True
    assert env_flag({'ARCHIVE_ENABLED': '  '}, 'ARCHIVE_ENABLED') is True
    assert env_flag({}, 'ARCHIVE_ENABLED', default=False) is False


# ── ffmpeg output forwarding ─────────────────────────────────────────────────

def test_forward_output_prefixes_every_line():
    src = io.StringIO('frame dropped\nspeed=1.0x\n')
    dest = io.StringIO()
    forward_output(src, dest)
    assert dest.getvalue() == '[ffmpeg] frame dropped\n[ffmpeg] speed=1.0x\n'
    assert src.closed


def test_spawn_ffmpeg_forwards_stdout_and_stderr_prefixed(capfd):
    # Any argv works — the pumps don't care that it isn't real ffmpeg.
    proc, pumps = spawn_ffmpeg([
        sys.executable, '-c',
        'import sys; print("to stdout"); print("to stderr", file=sys.stderr)',
    ])
    assert proc.wait(timeout=10) == 0
    for t in pumps:
        t.join(timeout=10)
    out, err = capfd.readouterr()
    assert '[ffmpeg] to stdout' in out
    assert '[ffmpeg] to stderr' in err


def test_spawn_ffmpeg_pipes_do_not_leak_to_parent_streams(capfd):
    proc, pumps = spawn_ffmpeg([sys.executable, '-c', 'print("hi")'])
    proc.wait(timeout=10)
    for t in pumps:
        t.join(timeout=10)
    out, _ = capfd.readouterr()
    # The raw line must only appear with the prefix, never bare.
    assert out.count('hi') == 1
    assert '[ffmpeg] hi' in out
    assert proc.stdout.closed and proc.stderr.closed
    assert isinstance(proc, subprocess.Popen)
