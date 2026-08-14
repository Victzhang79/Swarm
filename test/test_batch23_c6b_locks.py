"""30 号文批23 锁：C-6b 三件（批18 merge 面 LEAD 收口）。

- C-6b#1：go.work `_norm_use` 归一吃掉 `./svc`/`svc` 写法分叉——并存=go fatal 重复
  目录，而四面（add/prune/strip/merge）经归一全看不见。reconcile 检测归一碰撞 +
  WARNING + 自愈去重（保留首现行）。
- C-6b#3③：导入期闸从【手抄镜像常量】改【行为探针】——候选名逐个过真函数
  `_is_shared_manifest`，函数体与常量/臂表任一方向漂移都 ImportError。
- C-6b#5：`_GRADLE_DYNAMIC` 的裸 `file(` 冤杀合法静态写法（`file('gradle.properties')`）
  ⇒ reconcile 不补 include + merge 直通成员蒸发。收紧为 include 邻近上下文，判定
  收口 `_gradle_dynamic_hit` 单一事实源（三面同源）。
"""
from __future__ import annotations

import logging
import re

import pytest

import swarm.worker.workspace_manifest as wm


# ── C-6b#3③：闸读行为不读镜像 ─────────────────────────────

def test_gate_reads_function_behavior_not_constant(monkeypatch):
    """主锁：函数体与常量分叉时闸必须炸——monkeypatch `_is_shared_manifest` 让常量成员
    go.work 不再路由，闸（若真读行为）必 ImportError。旧镜像闸（读常量+字面量）会
    照常过=漂移静默。把闸改回读镜像，本锁红。"""
    import swarm.worker.sandbox as sb

    real = sb._is_shared_manifest

    def _drifted(rel_posix, content=None):
        if rel_posix.rsplit("/", 1)[-1].lower() == "go.work":
            return False
        return real(rel_posix, content)

    monkeypatch.setattr(sb, "_is_shared_manifest", _drifted)
    with pytest.raises(ImportError, match="go.work"):
        wm._assert_mergers_cover_routing()


def test_gate_rejects_dead_arm(monkeypatch):
    """反向 2：`_MERGERS` 塞进一个函数不路由的臂键 ⇒ 死臂必须 ImportError
    （臂存在≠接线，另一条漂移方向）。"""
    monkeypatch.setitem(wm._MERGERS, "zzz.deadarm", wm._merge_conservative_passthrough)
    with pytest.raises(ImportError, match="zzz.deadarm"):
        wm._assert_mergers_cover_routing()


def test_gate_passes_on_current_registry():
    """反向钉：当前注册表必须过闸（防闸恒炸使前两锁假绿）。"""
    wm._assert_mergers_cover_routing()


def test_gate_nested_package_json_content_branch_probed(monkeypatch):
    """R1 hunter F4：嵌套 package.json【内容分支】也在闸探针里——patch 掉内容分支
    （workspaces JSON 返 False），闸必须炸；路径档探针覆盖不到这条分支。"""
    import swarm.worker.sandbox as sb

    real = sb._is_shared_manifest

    def _drifted(rel_posix, content=None):
        if content is not None and "workspaces" in str(content):
            return False  # 内容分支腐化模拟
        return real(rel_posix, content)

    monkeypatch.setattr(sb, "_is_shared_manifest", _drifted)
    with pytest.raises(ImportError, match="workspaces"):
        wm._assert_mergers_cover_routing()


def test_gate_rejects_over_routing(monkeypatch):
    """R1 hunter F4 反向：嵌套无 workspaces 的 package.json 被路由=过路由必须炸。"""
    import swarm.worker.sandbox as sb

    real = sb._is_shared_manifest

    def _drifted(rel_posix, content=None):
        if content is not None and "/" in rel_posix and rel_posix.endswith("package.json"):
            return True  # 内容分支 fail-open 化模拟
        return real(rel_posix, content)

    monkeypatch.setattr(sb, "_is_shared_manifest", _drifted)
    with pytest.raises(ImportError, match=r"不该路由"):
        wm._assert_mergers_cover_routing()


# ── C-6b#5：file( 误杀收紧 ────────────────────────────────

_STATIC_WITH_FILE_CALL = """\
rootProject.name = 'demo'
def props = file('gradle.properties')   // 合法静态：读配置文件
include ':core'
"""

_DYNAMIC_INCLUDE_FILE = """\
rootProject.name = 'demo'
include file('generated-modules')       // include 邻近上下文=动态枚举信号
"""


def test_gradle_static_file_call_no_longer_misjudged():
    """主锁：静态 settings.gradle 里的 `file('...')` 不再冤判动态——merge 面必须真并
    成员（不是保守直通）。把 `file\\s*\\(` 加回裸标记，本锁红。"""
    local = _STATIC_WITH_FILE_CALL + "include ':api'\n"
    merged = wm.merge_shared_manifest(local, _STATIC_WITH_FILE_CALL, "settings.gradle")
    assert "include ':api'" in merged, \
        f"静态 file( 被冤判动态 ⇒ merge 直通丢成员（C-6b#5 原病灶）: {merged!r}"
    # reconcile 面同步：tmp 工程 settings 含 file() 静态调用，新模块目录必须照补 include
    assert wm._gradle_dynamic_hit(_STATIC_WITH_FILE_CALL) is None
    # R1 hunter F7：静态输入逐面 pin——probes 面也必须收成员（三面结论一致才是真同源）
    assert wm.manifest_member_probes("settings.gradle", _STATIC_WITH_FILE_CALL) == [
        ("core", "core")], "静态 file( 下 probes 面不收成员=三面结论分叉"


def test_gradle_commented_include_file_not_dynamic():
    """R1 hunter F3：`//` 注释剥除已下沉进 `_gradle_dynamic_hit` 本体——注释里写
    `// include file('x')` 三面都不判动态。剥除退回 merge 面私有，本锁红。"""
    text = "// include file('generated-modules')\nrootProject.name = 'd'\ninclude ':core'\n"
    assert wm._gradle_dynamic_hit(text) is None, "注释里的 include file( 冤判动态"
    assert wm.manifest_member_probes("settings.gradle", text) == [("core", "core")]
    merged = wm.merge_shared_manifest(text + "include ':api'\n", text, "settings.gradle")
    assert "include ':api'" in merged


def test_gradle_block_comment_between_include_and_file_dynamic():
    """R1 hunter F5：`include /* c */ file('x')` 块注释隔断仍是动态——收紧不能把
    旧裸 file( 抓得到的形态放进来（方向=语义风险，比冤杀差）。"""
    text = "rootProject.name = 'd'\ninclude /* generated */ file('mods')\n"
    assert wm._gradle_dynamic_hit(text) is not None, "块注释隔断的 include file( 漏抓"


def test_gradle_include_file_context_still_dynamic(caplog):
    """反向钉：`include file(...)` 邻近上下文仍判动态 ⇒ merge 保守直通 + WARNING
    （收紧不能把真动态放进来）。"""
    local = _DYNAMIC_INCLUDE_FILE + "include ':api'\n"
    with caplog.at_level(logging.WARNING):
        merged = wm.merge_shared_manifest(local, _DYNAMIC_INCLUDE_FILE, "settings.gradle")
    assert merged == _DYNAMIC_INCLUDE_FILE, "动态枚举文件必须保守直通（语义风险）"
    assert "include ':api'" not in merged
    assert any("动态 include" in r.message or "动态" in r.message for r in caplog.records), \
        "直通必须留 WARNING（缺席机读可辨）"


def test_gradle_dynamic_hit_is_single_source():
    """三面同源钉：reconcile/probes/merge 必须消费同一个 `_gradle_dynamic_hit`——
    对动态文件，probes 不收成员（删除面契约）且 merge 直通（加法面契约），两面的
    「动态与否」结论必须一致。"""
    dyn = "rootDir.eachDir { d -> include d.name }\ninclude ':core'\n"
    assert wm._gradle_dynamic_hit(dyn) is not None
    assert wm.manifest_member_probes("settings.gradle", dyn) == [], \
        "动态文件 probes 必须不收成员（与 merge 直通同判据）"


# ── C-6b#1：go.work 写法分叉重复检测+自愈 ──────────────────

def _mk_go_proj(tmp_path, gowork_text: str, modules: list[str]):
    (tmp_path / "go.work").write_text(gowork_text, encoding="utf-8")
    for m in modules:
        (tmp_path / m).mkdir(parents=True, exist_ok=True)
        (tmp_path / m / "go.mod").write_text("module x\n", encoding="utf-8")


def test_go_work_spelling_split_dup_healed(tmp_path, caplog):
    """主锁：`use ( ./svc )` 块 + `use svc` 单行并存=归一碰撞（go fatal 重复目录）。
    reconcile 必须自愈去重（保留首现）+ WARNING。摘去重块，本锁红。"""
    _mk_go_proj(tmp_path, "go 1.22\n\nuse (\n\t./svc\n)\n\nuse svc\n", ["svc"])
    with caplog.at_level(logging.WARNING):
        mods, adds = wm._reconcile_go_work(tmp_path, [])
    text = (tmp_path / "go.work").read_text(encoding="utf-8")
    uses = re.findall(r"(?m)^\s*(?:use\s+)?\.?/?svc\s*$", text)
    assert len(uses) == 1, f"写法分叉重复未去重（go fatal 复发）: {text!r}"
    assert mods == ["go.work"], "去重改动必须进 repaired 清单（否则不同步回沙箱/权威库）"
    assert any("C-6b#1" in r.message for r in caplog.records), "去重必须 WARNING 留痕"


def test_go_work_dup_emptying_block_cleans_husk(tmp_path):
    """边界：首现是单行、重复在块内 ⇒ 去重掏空 `use ( )` 残块必须整体移除
    （go 解析器对空块报错，与 strip 臂同法）。"""
    _mk_go_proj(tmp_path, "go 1.22\n\nuse svc\n\nuse (\n\t./svc\n)\n", ["svc"])
    wm._reconcile_go_work(tmp_path, [])
    text = (tmp_path / "go.work").read_text(encoding="utf-8")
    assert "use (" not in text, f"空 use() 残块未清理: {text!r}"
    assert len(re.findall(r"(?m)^\s*(?:use\s+)?\.?/?svc\s*$", text)) == 1


def test_go_work_no_dup_behavior_unchanged(tmp_path, caplog):
    """反向钉：无重复的 go.work 行为不变——新模块照补 use，零 C-6b#1 WARNING。"""
    _mk_go_proj(tmp_path, "go 1.22\n\nuse (\n\t./svc\n)\n", ["svc", "api"])
    with caplog.at_level(logging.WARNING):
        mods, adds = wm._reconcile_go_work(tmp_path, [])
    assert adds.get("go.work") == ["api"], f"新成员补漏被去重逻辑扰动: {adds}"
    text = (tmp_path / "go.work").read_text(encoding="utf-8")
    assert "use ./api" in text
    assert not any("C-6b#1" in r.message for r in caplog.records)


def test_go_work_dedup_never_eats_prefix_siblings(tmp_path):
    """R1 hunter F1（HIGH）：去重正则缺行尾锚时前缀兄弟行被吃/真重复行全删。
    svc + svc2 兄弟 + svc 写法分叉重复：去重后 svc2 行必须原样、svc 恰剩一处。"""
    _mk_go_proj(tmp_path,
                "go 1.22\n\nuse (\n\t./svc2\n\t./svc\n)\n\nuse svc\n\n"
                "replace (\n\t./svc => ../legacy/svc\n)\n",
                ["svc", "svc2"])
    wm._reconcile_go_work(tmp_path, [])
    text = (tmp_path / "go.work").read_text(encoding="utf-8")
    assert re.search(r"(?m)^\s*\.?/?svc2\s*$", text), f"前缀兄弟行 svc2 被误吃: {text!r}"
    assert len(re.findall(r"(?m)^\s*\.?/?svc\s*$", text)) == 1, \
        f"真重复行未去重或全删（成员蒸发）: {text!r}"
    # R2 双透镜同条（hunter R2-F2/reviewer L-3）：replace 块多 token 行逐字断存活
    # （缺 $ 时 `./svc => ...` 被吃前缀残留 `=> ...` 直接损坏 go.work）。
    assert "\t./svc => ../legacy/svc\n" in text, f"replace 块行被去重正则误吃: {text!r}"


def test_reconcile_gradle_dynamic_skip_warns(tmp_path, caplog):
    """R2 reviewer L-1：reconcile 动态跳过 WARNING 的接线 pin（新账必须有人消费）——
    含 eachDir 的 settings 触发跳过必须留 WARNING 且返回空。"""
    (tmp_path / "settings.gradle").write_text(
        "rootDir.eachDir { d -> include d.name }\n", encoding="utf-8")
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "build.gradle").write_text("plugins {}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        mods, adds = wm._reconcile_gradle(tmp_path, [])
    assert mods == [] and adds == {}
    assert any("动态枚举启发式" in r.message for r in caplog.records), \
        "动态跳过必须 WARNING 机读可辨（静默出口复发）"


def test_go_work_strip_prefix_sibling_safe():
    """R1 hunter F1 sibling：strip 臂（prune_manifest_members）块内删除同一缺锚形状——
    摘 svc 时 svc2 行必须原样保留。★svc2 必须排前★：strip 是 count=1 首命中即删，
    svc 排前时缺锚也「碰巧正确」（假绿夹具形状，MU-F 首跑实证）。"""
    text = "go 1.22\n\nuse (\n\t./svc2\n\t./svc\n)\n"
    new_text, removed = wm.prune_manifest_members(
        "go.work", text, lambda m: m != "svc")  # svc 目录不存在 ⇒ 摘
    assert removed == ["svc"]
    assert "./svc2" in new_text, f"strip 吃了前缀兄弟行: {new_text!r}"
    assert not re.search(r"(?m)^\s*\.?/?svc\s*$", new_text)


def test_go_work_quoted_dup_healed(tmp_path):
    """R1 hunter F2/reviewer L-1：引号形态 `use "./svc"` 也在归一面内（_norm_use 剥引号）
    ⇒ 去重正则必须同覆盖（检出但删不动=谎报自愈）。"""
    _mk_go_proj(tmp_path, 'go 1.22\n\nuse (\n\t"./svc"\n)\n\nuse svc\n', ["svc"])
    wm._reconcile_go_work(tmp_path, [])
    text = (tmp_path / "go.work").read_text(encoding="utf-8")
    assert len(re.findall(r'''(?m)^\s*(?:use\s+)?["']?\.?/?svc["']?\s*$''', text)) == 1, \
        f"引号形态重复未去重: {text!r}"


def test_go_work_uncoverable_form_honest_downgrade(tmp_path, caplog):
    """R1 hunter F2：行形态未覆盖（内联 `use (./svc)`）时绝不谎报「已自愈」——
    机读可辨降级 WARNING 且文件不虚报进 modified。"""
    _mk_go_proj(tmp_path, "go 1.22\n\nuse (./svc)\n\nuse svc\n", ["svc"])
    with caplog.at_level(logging.WARNING):
        mods, _adds = wm._reconcile_go_work(tmp_path, [])
    assert any("未自愈" in r.message for r in caplog.records), \
        "检出但删不动时必须诚实降级（检出≠自愈，机读可辨）"
    assert mods == [], "未自愈不得把文件虚报进 modified_manifests"
