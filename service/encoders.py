"""
encoders.py -- the single ffmpeg H.264 encoder-capability probe.

`nvenc_works()` answers "can h264_nvenc actually encode on this host?" with a
one-frame test encode (the encoder can be compiled into ffmpeg while the GPU
or driver libs are absent, so listing `-encoders` is not enough).

Both consumers share this probe but want different missing-ffmpeg behaviour:

  pipeline.py         nvenc_works(require_ffmpeg=True) -- no ffmpeg means the
                      service cannot run at all: print an error and exit 1.
  video_transcode.py  nvenc_works() -- treat missing ffmpeg as "no NVENC" and
                      let its own encoder cascade pick a fallback.

The per-use-case encoder *argument* builders stay with their domains and are
deliberately not unified -- live/archive args in archive_encoder.py and
stream_command.py, /video assembly args in video_transcode.py.
"""
import functools
import subprocess
import sys

_PROBE_CMD = ['ffmpeg', '-hide_banner',
              '-f', 'lavfi', '-i', 'nullsrc=s=64x64:d=0.04',
              '-frames:v', '1', '-c:v', 'h264_nvenc', '-f', 'null', '-']
_PROBE_TIMEOUT_SEC = 15


@functools.lru_cache(maxsize=None)
def nvenc_works(require_ffmpeg=False):
    """True when h264_nvenc can actually encode (driver libs present).

    With require_ffmpeg=True a missing ffmpeg binary is fatal (message on
    stderr, exit 1) instead of reading as "no GPU".
    """
    try:
        r = subprocess.run(_PROBE_CMD, capture_output=True,
                           timeout=_PROBE_TIMEOUT_SEC)
        return r.returncode == 0
    except FileNotFoundError:
        if require_ffmpeg:
            print('[service] ERROR: ffmpeg not found in PATH', file=sys.stderr)
            sys.exit(1)
        return False
    except subprocess.TimeoutExpired:
        return False
