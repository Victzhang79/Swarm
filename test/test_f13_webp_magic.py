#!/usr/bin/env python3
"""F13（round28，小）：WebP 魔数只校验 `RIFF` 前缀 → 任意 RIFF 容器(wav/avi/…)伪装成 .webp 通过。

治本：WebP 头 = `RIFF`(0..4) + 4字节长度 + `WEBP`(8..12)，须同时校验 form-type。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.api.routers.upload import _check_magic


def _riff(form: bytes) -> bytes:
    # RIFF + 4字节长度(任意) + form-type
    return b"RIFF" + b"\x00\x00\x00\x00" + form


def test_real_webp_passes():
    assert _check_magic(".webp", _riff(b"WEBP") + b"VP8 ") is True


def test_non_webp_riff_rejected():
    # wav 也是 RIFF 容器（form=WAVE）——旧逻辑只看 RIFF 会误放行
    assert _check_magic(".webp", _riff(b"WAVE")) is False
    assert _check_magic(".webp", _riff(b"AVI ")) is False
    assert _check_magic(".webp", b"RIFF") is False  # 太短，无 form-type


def test_other_ext_unaffected():
    assert _check_magic(".png", b"\x89PNG\r\n") is True
    assert _check_magic(".png", b"NOTPNG") is False
    assert _check_magic(".txt", b"anything") is True  # 文本类无魔数要求


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("F13 webp magic 单测通过。")
