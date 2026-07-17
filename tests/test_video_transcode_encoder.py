"""
Unit tests for video_transcode._detect_encoder_args().

The selection cascade (h264_nvenc → libx264 → mpeg4 → ffv1) and the chosen
flags determine the quality of the assembled /video output.  These tests
mock subprocess.run so we can exercise every branch without ffmpeg present.

The expectation: every preferred branch is quality-targeted (CRF / NVENC
constqp), so a near-lossless archive isn't quietly downgraded on download.
"""

import pytest

import video_transcode


def _stub_encoders_listing(monkeypatch, encoders_text, nvenc_works=False):
    """Make subprocess.run return a fake `ffmpeg -encoders` listing."""
    class _Result:
        returncode = 0
        stdout     = encoders_text
        stderr     = ''
    monkeypatch.setattr(video_transcode.subprocess, 'run',
                        lambda *a, **kw: _Result())
    monkeypatch.setattr(video_transcode, '_nvenc_works', lambda: nvenc_works)


@pytest.fixture(autouse=True)
def _force_default_qp(monkeypatch):
    """Pin _VIDEO_QP to its documented default so the assertions are stable.

    The module reads VIDEO_QP / ARCHIVE_QP at import time; tests below assume
    QP=18.  Reset it in case earlier-running tests altered the environment.
    """
    monkeypatch.setattr(video_transcode, '_VIDEO_QP', 18)


def test_nvenc_branch_uses_constqp_18(monkeypatch):
    _stub_encoders_listing(monkeypatch, 'h264_nvenc libx264', nvenc_works=True)
    args = video_transcode._detect_encoder_args()
    assert args == ['-c:v', 'h264_nvenc', '-preset', 'p5',
                    '-rc', 'constqp', '-qp', '18']


def test_libx264_branch_uses_crf(monkeypatch):
    _stub_encoders_listing(monkeypatch, 'libx264 mpeg4', nvenc_works=False)
    args = video_transcode._detect_encoder_args()
    assert args == ['-c:v', 'libx264', '-preset', 'medium',
                    '-crf', '18', '-pix_fmt', 'yuv420p']


def test_nvenc_listed_but_nonfunctional_falls_back_to_libx264(monkeypatch):
    """h264_nvenc may be compiled in but the GPU absent — must still fall back."""
    _stub_encoders_listing(monkeypatch, 'h264_nvenc libx264 mpeg4',
                           nvenc_works=False)
    args = video_transcode._detect_encoder_args()
    assert args[:2] == ['-c:v', 'libx264']
    assert '-crf' in args


def test_no_modern_encoder_falls_back_to_mpeg4(monkeypatch):
    _stub_encoders_listing(monkeypatch, 'mpeg4 ffv1', nvenc_works=False)
    args = video_transcode._detect_encoder_args()
    assert args == ['-c:v', 'mpeg4', '-q:v', '5']


def test_no_encoders_falls_back_to_ffv1(monkeypatch):
    _stub_encoders_listing(monkeypatch, 'ffv1', nvenc_works=False)
    args = video_transcode._detect_encoder_args()
    assert args == ['-c:v', 'ffv1']


def test_ffmpeg_missing_falls_back_to_libx264_crf(monkeypatch):
    """When `ffmpeg -encoders` itself fails, we still emit a quality target."""
    def _raise(*_a, **_kw):
        raise FileNotFoundError(2, 'ffmpeg', '')
    monkeypatch.setattr(video_transcode.subprocess, 'run', _raise)
    args = video_transcode._detect_encoder_args()
    assert args == ['-c:v', 'libx264', '-preset', 'medium',
                    '-crf', '18', '-pix_fmt', 'yuv420p']


def test_video_qp_override_propagates(monkeypatch):
    """Setting _VIDEO_QP changes the CRF/QP value the cascade emits."""
    monkeypatch.setattr(video_transcode, '_VIDEO_QP', 22)
    _stub_encoders_listing(monkeypatch, 'libx264', nvenc_works=False)
    args = video_transcode._detect_encoder_args()
    assert '22' in args
    assert '-crf' in args
