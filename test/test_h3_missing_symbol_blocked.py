#!/usr/bin/env python3
"""H-3a（round67j 法证A 横切病灶）：缺符号失败统一 BLOCKED 口径。

死因实锤（round67 task 64cb44ed L1310）：st-50-1 引 ISysGoogleAuthService（包
com.ruoyi.system.service 已存在、类正是 st-8-1 的 create 目标未落地）→ javac 报
「cannot find symbol: class C / location: package P」→ 走不进 ②(unbuilt_internal 只认
"package does not exist" 且判"包已在树=真错 FAIL") → hard fail 连烧修复轮弃修；
同型整包缺失的 st-48 却判 BLOCKED 退避——同一"生产者未落地"两种结局。
治=类级判据 sibling（_build_blocked_on_unbuilt_internal_classes），同分类同消费链。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from worker.l1_parse import parse_missing_symbol_classes  # noqa: E402
from worker.l1_pipeline import _build_blocked_on_unbuilt_internal_classes  # noqa: E402

# round67 真实形态（Maven [ERROR] 前缀三行组）
_MVN_OUT = """\
[ERROR] /ws/ruoyi-admin/src/main/java/com/ruoyi/web/controller/system/GoogleAuthController.java:[12,45] cannot find symbol
[ERROR]   symbol:   class ISysGoogleAuthService
[ERROR]   location: package com.ruoyi.system.service
"""

# 裸 javac 形态（无 [ERROR] 前缀）
_JAVAC_OUT = """\
/ws/App.java:12: error: cannot find symbol
  symbol:   interface IAlarmService
  location: package com.ruoyi.alarm.service
"""

# location: class（成员缺失=真编译错，绝不收）
_MEMBER_OUT = """\
[ERROR] /ws/App.java:[9,8] cannot find symbol
[ERROR]   symbol:   method doIt()
[ERROR]   location: class com.ruoyi.alarm.service.AlarmService
"""


# ── parser 纯函数 ──

def test_parse_maven_error_prefix_form():
    assert parse_missing_symbol_classes(_MVN_OUT) == [
        ("ISysGoogleAuthService", "com.ruoyi.system.service")]


def test_parse_bare_javac_form():
    assert parse_missing_symbol_classes(_JAVAC_OUT) == [
        ("IAlarmService", "com.ruoyi.alarm.service")]


def test_parse_member_location_class_not_collected():
    """location: class（成员缺失）与无 symbol 行的输出都不收——只认类级 package 形态。"""
    assert parse_missing_symbol_classes(_MEMBER_OUT) == []
    assert parse_missing_symbol_classes("") == []
    assert parse_missing_symbol_classes("BUILD FAILURE unrelated") == []


def test_parse_dedup_keeps_order():
    out = parse_missing_symbol_classes(_MVN_OUT + _MVN_OUT + _JAVAC_OUT)
    assert out == [("ISysGoogleAuthService", "com.ruoyi.system.service"),
                   ("IAlarmService", "com.ruoyi.alarm.service")]


# ── gate 判据（tmp 树真 grep）──

def _mk_tree(tmp_path, files: dict[str, str]):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def _own(monkeypatch, pkgs):
    import worker.l1_pipeline as _lp
    monkeypatch.setattr(_lp, "_project_own_packages", lambda *a, **k: set(pkgs))


def test_gate_blocks_when_internal_class_unbuilt(tmp_path, monkeypatch):
    """★H-3a 主治★ 包在树里（有别的类）、缺的类未建出 → 判 BLOCKED（返回其包）。"""
    root = _mk_tree(tmp_path, {
        "ruoyi-system/src/main/java/com/ruoyi/system/service/ISysUserService.java":
            "package com.ruoyi.system.service;\npublic interface ISysUserService {}\n"})
    _own(monkeypatch, ["com.ruoyi"])
    got = _build_blocked_on_unbuilt_internal_classes(root, _MVN_OUT, 20)
    assert got == {"com.ruoyi.system.service"}


def test_gate_fails_when_class_already_in_tree(tmp_path, monkeypatch):
    """类已在树里却报缺符号 = 真编译错（classpath/模块序）→ 空集照常 FAIL（绝不误标）。"""
    root = _mk_tree(tmp_path, {
        "ruoyi-system/src/main/java/com/ruoyi/system/service/ISysGoogleAuthService.java":
            "package com.ruoyi.system.service;\npublic interface ISysGoogleAuthService {}\n"})
    _own(monkeypatch, ["com.ruoyi"])
    assert _build_blocked_on_unbuilt_internal_classes(root, _MVN_OUT, 20) == set()


def test_gate_fails_on_third_party_symbol(tmp_path, monkeypatch):
    """缺符号属第三方命名空间 → 空集照常 FAIL（交 dep-repair，绝不吞真错）。"""
    _own(monkeypatch, ["com.ruoyi"])
    out = _MVN_OUT.replace("com.ruoyi.system.service", "org.springframework.stereotype")
    assert _build_blocked_on_unbuilt_internal_classes(str(tmp_path), out, 20) == set()


def test_gate_empty_on_no_symbol_errors(tmp_path, monkeypatch):
    _own(monkeypatch, ["com.ruoyi"])
    assert _build_blocked_on_unbuilt_internal_classes(str(tmp_path), "BUILD FAILURE", 20) == set()
