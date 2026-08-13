#!/usr/bin/env python3
"""30 号文批8 C-6 锁：共享清单合并器注册表收口 + 三类成员并集臂。

被锁缺陷：路由集（`_is_shared_manifest` 为 True 的 basename）7+3 项而合并分派 if 链
只认 4 项 ⇒ settings.gradle 的 include / go.work 的 use / .sln 的 Project 在 pull-back
时静默 `return incoming_text` ⇒ 并行兄弟的成员注册整份蒸发（R48c-1 last-write-wins
换 basename 复发；flock 正确串行化之后，丢失发生在锁内的合并逻辑里）。
治法：`_MERGERS` 显式注册表 + 导入期断言路由集⊆表（缺臂=ImportError）+ 三类【只并
成员不并依赖】臂（gradle 依赖区 Groovy DSL 顾虑照留=build.gradle 注册为刻意保守直通）。
"""

from __future__ import annotations

import logging

import pytest

import swarm.worker.workspace_manifest as wm
from swarm.worker.workspace_manifest import merge_shared_manifest


# ─── 1. 导入期闸接线锁：路由集 ⊄ _MERGERS 时断言必须炸 ───

def test_import_gate_fires_when_arm_missing(monkeypatch):
    """删掉任一合并臂 → `_assert_mergers_cover_routing` 必须 ImportError。
    判据：本测试红 = 导入期闸是死代码（机制存在≠接线生效）。"""
    broken = {k: v for k, v in wm._MERGERS.items() if k != "go.work"}
    monkeypatch.setattr(wm, "_MERGERS", broken)
    with pytest.raises(ImportError, match="go.work"):
        wm._assert_mergers_cover_routing()


def test_import_gate_passes_on_current_registry():
    """当前注册表必须过闸（反向锁：防闸本身恒炸/恒过）。"""
    wm._assert_mergers_cover_routing()


# ─── 2. 分派走注册表（接线锁，非实现锁）───

def test_dispatch_goes_through_registry(monkeypatch):
    """monkeypatch 注册表项 → 分派必须调用它（断「分派绕开注册表」）。"""
    seen: list[tuple[str, str, str]] = []

    def _spy(l, i, r, base_dir=None):
        seen.append((l, i, r))
        return i

    monkeypatch.setitem(wm._MERGERS, "go.work", _spy)
    assert merge_shared_manifest("use ./a\n", "use ./b\n", "go.work") == "use ./b\n"
    assert seen == [("use ./a\n", "use ./b\n", "go.work")]


# ─── 3. settings.gradle(.kts)：local 独有 include 并回（C-6 主实证面）───

def test_settings_gradle_member_merged_back(caplog):
    incoming = "rootProject.name = 'demo'\ninclude ':mod-a'\n"
    local = "rootProject.name = 'demo'\ninclude ':mod-a'\ninclude ':mod-local-only'\n"
    with caplog.at_level(logging.INFO, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "settings.gradle")
    assert "include ':mod-a'" in merged
    assert "include ':mod-local-only'" in merged, \
        "local 独有 include 整份蒸发 = C-6 原病（merged==incoming）"
    assert merged != incoming


def test_settings_gradle_kts_member_merged_back():
    incoming = 'rootProject.name = "demo"\ninclude(":mod-a")\n'
    local = 'rootProject.name = "demo"\ninclude(":mod-a")\ninclude(":mod-local-only")\n'
    merged = merge_shared_manifest(local, incoming, "settings.gradle.kts")
    assert 'include(":mod-local-only")' in merged, "kts 形态必须按 kts 语法并回"


def test_gradle_dynamic_include_passthrough(caplog):
    """动态枚举（fileTree/listFiles 等）→ 保守直通 + WARNING（与 reconcile 同判据，
    文本级加法对动态枚举有语义风险）。"""
    incoming = "rootProject.name = 'demo'\ninclude ':mod-a'\n"
    local = ("rootProject.name = 'demo'\ninclude ':mod-a'\n"
             "fileTree(dir: 'mods').each { include it.name }\n")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "settings.gradle")
    assert merged == incoming
    assert any("动态 include" in r.message for r in caplog.records)


# ─── 4. go.work：local 独有 use 并回 ───

def test_go_work_member_merged_back():
    incoming = "go 1.22\n\nuse (\n\t./svc-a\n)\n"
    local = "go 1.22\n\nuse (\n\t./svc-a\n\t./svc-local-only\n)\n"
    merged = merge_shared_manifest(local, incoming, "go.work")
    assert "./svc-a" in merged
    assert "./svc-local-only" in merged, "local 独有 use 蒸发 = C-6 原病"


def test_go_work_dedup_no_double_use():
    """两侧都有的成员绝不重复追加（go.work 对重复 use 硬错）。"""
    text = "go 1.22\n\nuse ./svc-a\n"
    assert merge_shared_manifest(text, text, "go.work") == text


# ─── 5. .sln：local 独有 Project 对并回（块+配置行；GUID 随 local 原块）───

_SLN_HEAD = (
    'Microsoft Visual Studio Solution File, Format Version 12.00\n'
    '# Visual Studio Version 17\n'
)
_SLN_PROJ_A = (
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "AppA", "src\\AppA\\AppA.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"\n'
    'EndProject\n'
)
_SLN_PROJ_B = (
    'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "AppB", "src\\AppB\\AppB.csproj", "{BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB}"\n'
    'EndProject\n'
)
_SLN_GLOBAL = (
    'Global\n'
    '\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n'
    '\t\t{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}.Debug|Any CPU.ActiveCfg = Debug|Any CPU\n'
    '\tEndGlobalSection\n'
    'EndGlobal\n'
)


def test_sln_project_merged_back():
    incoming = _SLN_HEAD + _SLN_PROJ_A + _SLN_GLOBAL
    local = _SLN_HEAD + _SLN_PROJ_A + _SLN_PROJ_B + _SLN_GLOBAL
    merged = merge_shared_manifest(local, incoming, "demo.sln")
    assert 'AppB.csproj' in merged, "local 独有 Project 蒸发 = C-6 原病"
    assert '{BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB}' in merged, "local 原块 GUID 必须随块保留"
    assert '{BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB}.Debug|Any CPU.ActiveCfg' in merged, \
        "只插 Project 块漏配置行 = 损坏 sln（VS/msbuild 确定性失败）"


def test_sln_missing_cfg_section_passthrough(caplog):
    """缺 ProjectConfigurationPlatforms 段 → 整体不并 + WARNING（绝不产损坏 sln）。"""
    incoming = _SLN_HEAD + _SLN_PROJ_A + 'Global\nEndGlobal\n'
    local = _SLN_HEAD + _SLN_PROJ_A + _SLN_PROJ_B + 'Global\nEndGlobal\n'
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "demo.sln")
    assert merged == incoming
    assert any("ProjectConfigurationPlatforms" in r.message for r in caplog.records)


# ─── 6. build.gradle(.kts)：刻意保守直通（登记的不并）───

def test_build_gradle_conservative_passthrough(caplog):
    """依赖区是 Groovy/Kotlin DSL，文本级并集无语义保证——直通是【登记的决定】，
    内容分叉必须留 INFO（不并≠无损失面，损失如实可观测）。"""
    incoming = "plugins { id 'java' }\n"
    local = "plugins { id 'java' }\ndependencies { implementation 'x:y:1' }\n"
    with caplog.at_level(logging.INFO, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "build.gradle")
    assert merged == incoming
    assert any("保守直通" in r.message for r in caplog.records), \
        "内容分叉直通零留痕 = 硬检查④违反（降级不可观测）"


# ─── 7. 表外调用：WARNING + 直通（不静默）───

def test_unregistered_basename_passthrough_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest("a\n", "b\n", "Makefile")
    assert merged == "b\n"
    assert any("_MERGERS" in r.message for r in caplog.records)


# ─── 8. pom 臂抽取后行为回归（成员并回不受影响）───

def test_pom_member_still_merged_after_extraction():
    incoming = ("<project>\n  <modules>\n    <module>mod-a</module>\n  </modules>\n"
                "</project>\n")
    local = ("<project>\n  <modules>\n    <module>mod-a</module>\n"
             "    <module>mod-local-only</module>\n  </modules>\n</project>\n")
    merged = merge_shared_manifest(local, incoming, "pom.xml")
    assert "<module>mod-local-only</module>" in merged, \
        "pom 臂抽进注册表后行为必须逐字不变（叶簇拆分纪律）"


# ─── 9. 批8 R1 reviewer MEDIUM：多 token include 并回 + 去重不产重复 ───

def test_gradle_multi_token_include_merged_back():
    """probes 的「单 token 独占一行」边界是删除面契约；merge 是加法面——
    `include ':a', ':b'` 多 token 行跳过=并行兄弟注册整份蒸发（消费契约分档）。"""
    incoming = "rootProject.name = 'demo'\n"
    local = "rootProject.name = 'demo'\ninclude ':a', ':b'\n"
    merged = merge_shared_manifest(local, incoming, "settings.gradle")
    assert "include ':a'" in merged and "include ':b'" in merged, \
        "多 token include 整行蒸发 = reviewer MEDIUM 原病"


def test_gradle_multi_token_dedup_no_duplicate_include():
    """incoming 多 token 行已含 ':a'，local 又单行写 ':a' → 去重后绝不追加重复
    （重复 include = gradle duplicate project 解析错）。"""
    incoming = "rootProject.name = 'demo'\ninclude ':a', ':b'\n"
    local = "rootProject.name = 'demo'\ninclude ':a', ':b'\ninclude ':a'\n"
    merged = merge_shared_manifest(local, incoming, "settings.gradle")
    assert merged.count("include ':a'") == 1, f"重复 include: {merged}"


# ─── 10. 批8 R1 hunter #5：注释里的 file(/rootDir 不冤判动态 ───

def test_gradle_comment_with_dynamic_substring_not_misjudged(caplog):
    """merge 面跳过=丢注册（比 reconcile 面重）——`// see file(x)` 这类注释子串
    不得把静态 settings.gradle 冤判成动态枚举而直通。"""
    incoming = "rootProject.name = 'demo'\ninclude ':mod-a'\n"
    local = ("rootProject.name = 'demo'\ninclude ':mod-a'\n"
             "include ':mod-b'  // see file(xxx) for layout\n")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "settings.gradle")
    assert "include ':mod-b'" in merged, "注释子串冤判动态 = 合法注册被直通丢掉"
    assert not [r for r in caplog.records if "动态 include" in r.message]


def test_gradle_dynamic_warning_carries_matched_substring(caplog):
    """真动态直通时 WARNING 必须带命中子串（定位误杀/真命中一眼可辨）。"""
    incoming = "rootProject.name = 'demo'\ninclude ':a'\n"
    local = ("rootProject.name = 'demo'\ninclude ':a'\n"
             "fileTree(dir: 'mods').each { include it.name }\n")
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        assert merge_shared_manifest(local, incoming, "settings.gradle") == incoming
    msgs = [r.message for r in caplog.records if "动态 include" in r.message]
    assert msgs and "fileTree" in msgs[0], f"WARNING 缺命中子串: {msgs}"


# ─── 11. 批8 R1 hunter #4：sln GUID 撞车 fail-closed + 未知类型跳过留痕 ───

def test_sln_guid_collision_skipped_failclosed(caplog):
    """local 块 GUID 与 incoming 既有块撞 → 跳过+WARNING，绝不产 GUID 重复的无效 sln。"""
    proj_b_dup_guid = (
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "AppB", '
        '"src\\\\AppB\\\\AppB.csproj", "{AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA}"\n'
        'EndProject\n'
    )
    incoming = _SLN_HEAD + _SLN_PROJ_A + _SLN_GLOBAL
    local = _SLN_HEAD + _SLN_PROJ_A + proj_b_dup_guid + _SLN_GLOBAL
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "demo.sln")
    assert "AppB" not in merged, "GUID 撞车块必须 fail-closed 跳过（宁缺不产坏文件）"
    assert any("GUID" in r.message and "撞车" in r.message for r in caplog.records)


def test_sln_unknown_type_skip_leaves_trace(caplog):
    """解决方案文件夹/未知类型工程按契约不并，但必须 WARNING 留痕（缺席机读可辨）。"""
    folder = (
        'Project("{2150E333-8FDC-42A3-9474-1A3956D46DE8}") = "docs", "docs", '
        '"{CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC}"\nEndProject\n'
    )
    incoming = _SLN_HEAD + _SLN_PROJ_A + _SLN_GLOBAL
    local = _SLN_HEAD + _SLN_PROJ_A + folder + _SLN_GLOBAL
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest(local, incoming, "demo.sln")
    assert "docs" not in merged
    assert any("非已知类型" in r.message for r in caplog.records), \
        "未知类型跳过零留痕 = 加法-only 承诺静默缺一角"


# ─── 12. 批8 R1 hunter #2：异常 fail-open 升 ERROR+exc_type，与政策性 WARNING 可分 ───

def test_arm_exception_is_error_not_policy_warning(monkeypatch, caplog):
    def _boom(l, i, r, base_dir=None):
        raise RuntimeError("simulated parse bug")

    monkeypatch.setitem(wm._MERGERS, "settings.gradle", _boom)
    with caplog.at_level(logging.WARNING, logger="swarm.worker.workspace_manifest"):
        merged = merge_shared_manifest("include ':a'\n", "include ':b'\n",
                                       "settings.gradle")
    assert merged == "include ':b'\n", "fail-open 形状不变（返 incoming）"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and "exc_type=RuntimeError" in errors[0].message, \
        "异常 fail-open 必须 ERROR+exc_type——与政策性 WARNING 混一起运维分不出"


# ─── 13. 批8 R1 hunter #3①：导入期闸的【调用行】接线锁 ───

def test_import_gate_actually_executes_at_module_import(monkeypatch):
    """删掉模块底部的 `_assert_mergers_cover_routing()` 调用行 → 本测试红：
    往路由集塞一个无臂 basename 后 reload 模块，导入期必须 ImportError
    （闸函数存在≠被调用——接线覆盖≠机制存在）。"""
    import importlib

    import swarm.worker.sandbox as sb
    monkeypatch.setattr(
        sb, "_SHARED_MANIFEST_BASENAMES",
        sb._SHARED_MANIFEST_BASENAMES | {"probe.nomerge"})
    with pytest.raises(ImportError, match="probe.nomerge"):
        importlib.reload(wm)
    # 恢复干净模块态（monkeypatch 先撤销，再 reload 回真注册表）
    monkeypatch.undo()
    importlib.reload(wm)
