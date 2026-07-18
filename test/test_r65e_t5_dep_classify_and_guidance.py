"""R65E-T5（round65e5 st-53-1 架构治本 D2/D3）：A2 缺依赖恢复对【无坐标幻觉包】的精准分流 + 防手写 pom 腐化。

死因（六轮硬取证，见 journal）：st-53-1 终局主因=totp 1.7.1 API 幻觉（能力墙），每轮死于不同臆造
API；其中 R3 `package dev.samstevens.totp.generator does not exist`=**jar 在 classpath、子包臆造**
（同编译里 .util/.code/.secret/.time 全解析 OK）。A2 恢复却把它当"缺外部 jar"，找不到坐标（D1 后不
再误绑 ruoyi-generator）仍授 pom 写权+重派——(a)R2 实锤：授 pom 写权后小模型把 `<groupId>` 手写成
`<group>`、毁 `<parent>` → 整树读不出（瞬态但真实）；(b)对幻觉包补依赖永远救不了。

D2：`classify_missing_deps_for_stack` 把缺失包分【可自证补全 provisionable】/【全仓无坐标 unprovisioned】
两类（不 mutate）。D3+D2 guidance：`_dep_recovery_retry_guidance` 给授权子任务构造重派 guidance——
D3 铁律恒给（勿改 pom 结构/绝不重写 <groupId>/<parent>）；unprovisioned 附"改代码别加依赖"。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import swarm.brain.nodes as N
from swarm.brain.nodes.maven_repair import (
    _stack_driver_keys,
    classify_missing_deps_for_stack,
)
from swarm.brain.nodes.failure import (
    _D2_UNPROVISIONED_TMPL,
    _D3_POM_IRON_LAW,
    _dep_recovery_retry_guidance,
    _merge_dep_guidance_lines,
)


def _proj_with_quartz_sibling() -> str:
    """迷你工程：兄弟 pom 声明外部坐标 org.quartz:quartz；失败模块 mod-x/pom.xml。"""
    d = tempfile.mkdtemp()
    root = Path(d)
    (root / "sched").mkdir()
    (root / "sched" / "pom.xml").write_text(
        "<project><dependencies>\n"
        "  <dependency><groupId>org.quartz-scheduler</groupId><artifactId>quartz</artifactId>"
        "<version>2.3.2</version></dependency>\n"
        "</dependencies></project>\n")
    (root / "mod-x").mkdir()
    (root / "mod-x" / "pom.xml").write_text(
        "<project><artifactId>mod-x</artifactId><dependencies>\n"
        "  <dependency><groupId>com.x</groupId><artifactId>common</artifactId></dependency>\n"
        "</dependencies></project>\n")
    return d


# ── D2 分类 driver ──
def test_classify_splits_provisionable_vs_unprovisioned():
    """★D2 核心★ 缺失包分流：org.quartz 有兄弟自证坐标→provisionable；
    dev.samstevens.totp.generator 全仓无坐标（幻觉子包）→unprovisioned。"""
    d = _proj_with_quartz_sibling()
    granted = {"st-53-1": "mod-x/pom.xml"}
    results = {"st-53-1": {"l1_details": {"build_output":
               "[ERROR] package org.quartz does not exist\n"
               "[ERROR] package dev.samstevens.totp.generator does not exist"}}}
    cls = classify_missing_deps_for_stack({"build": "maven"}, d, granted, results)
    assert "st-53-1" in cls, f"应分类 st-53-1；实得: {cls}"
    prov = cls["st-53-1"]["provisionable"]
    unprov = cls["st-53-1"]["unprovisioned"]
    assert "org.quartz" in prov, f"quartz 应可自证补全；实得 provisionable={prov}"
    assert "dev.samstevens.totp.generator" in unprov, \
        f"幻觉子包应判 unprovisioned；实得 unprovisioned={unprov}"


def test_classify_hallucinated_subpkg_all_unprovisioned():
    """幻觉包全仓无坐标（D1 后不再误绑内部 ruoyi-generator）→ 全落 unprovisioned、provisionable 空。"""
    d = _proj_with_quartz_sibling()
    granted = {"st-53-1": "mod-x/pom.xml"}
    results = {"st-53-1": {"l1_details": {"build_output":
               "package dev.samstevens.totp.generator does not exist"}}}
    cls = classify_missing_deps_for_stack({"build": "maven"}, d, granted, results)
    assert cls["st-53-1"]["provisionable"] == []
    assert cls["st-53-1"]["unprovisioned"] == ["dev.samstevens.totp.generator"]


def test_classify_no_project_path_empty():
    assert classify_missing_deps_for_stack({"build": "maven"}, None, {"s": "p/pom.xml"}, {}) == {}


def test_classify_reexport_addressable():
    """经 __init__ re-export，swarm.brain.nodes.classify_missing_deps_for_stack 可寻址。"""
    assert getattr(N, "classify_missing_deps_for_stack", None) is classify_missing_deps_for_stack


# ── refactor 锁：_stack_driver_keys 口径（inject/classify 共用，不得分叉） ──
def test_stack_driver_keys_behavior():
    assert _stack_driver_keys({"build": "gradle"}) == ["gradle"]
    assert _stack_driver_keys({}) == ["maven"]          # 无画像退回 maven（旧行为）
    assert _stack_driver_keys(None) == ["maven"]
    # backend 自由文本按 driver 键子串探测（maven 是已注册 driver 键）
    assert "maven" in _stack_driver_keys({"backend": "Spring Boot 2.x (maven)"})


# ── D2/D3 guidance 纯函数 ──
def test_guidance_ironlaw_always_for_granted():
    """★D3★ 授 pom 写权的每个子任务恒得"最小增量铁律"（防 <group>/毁 <parent> 腐化）。"""
    granted = {"st-53-1": "ruoyi-framework/pom.xml"}
    g = _dep_recovery_retry_guidance(granted, {})
    assert "st-53-1" in g
    txt = g["st-53-1"]
    assert "pom" in txt and ("<group" in txt or "parent" in txt.lower()), \
        f"D3 铁律缺失；实得: {txt}"


def test_guidance_unprovisioned_adds_codefix_hint():
    """★D2★ 有 unprovisioned 幻觉包 → guidance 追加"改代码别加依赖"且点名该包。"""
    granted = {"st-53-1": "ruoyi-framework/pom.xml"}
    cls = {"st-53-1": {"provisionable": [], "unprovisioned": ["dev.samstevens.totp.generator"]}}
    g = _dep_recovery_retry_guidance(granted, cls)
    txt = g["st-53-1"]
    assert "dev.samstevens.totp.generator" in txt, "应点名幻觉包"
    assert ("勿" in txt or "不要" in txt or "别" in txt or "不" in txt) and "依赖" in txt, \
        f"应含'别加依赖'语义；实得: {txt}"
    # 仍含 D3 铁律（两者叠加）
    assert "parent" in txt.lower() or "<group" in txt


def test_guidance_provisionable_only_gets_ironlaw_no_codefix():
    """provisionable-only（真缺依赖、已确定性补好）→ 只给 D3 铁律，不给 D2"别加依赖"（那会误导）。"""
    granted = {"st-9": "sched/pom.xml"}
    cls = {"st-9": {"provisionable": ["org.quartz"], "unprovisioned": []}}
    g = _dep_recovery_retry_guidance(granted, cls)
    txt = g["st-9"]
    assert "不要" not in txt.replace("绝不", ""), \
        f"provisionable-only 不该出现'不要加依赖'误导；实得: {txt}"
    assert "parent" in txt.lower() or "<group" in txt  # D3 铁律仍在


def test_guidance_unprovisioned_only_omits_false_added_claim():
    """★复核 HIGH 锁★ unprovisioned-only 且本轮无注入 → 【绝不】声称"依赖已补"（假前提，会与 D2
    '此包全仓无坐标'自相矛盾、把假硬约束喂给 worker）。"""
    granted = {"st-53-1": "ruoyi-framework/pom.xml"}
    cls = {"st-53-1": {"provisionable": [], "unprovisioned": ["dev.samstevens.totp.generator"]}}
    g = _dep_recovery_retry_guidance(granted, cls, injected={})
    txt = g["st-53-1"]
    assert "依赖已补" not in txt, f"unprovisioned-only 不该声称已补入依赖；实得: {txt}"
    assert "dev.samstevens.totp.generator" in txt        # D2 仍在
    assert "parent" in txt.lower() or "<group" in txt     # D3 铁律仍在


def test_guidance_injected_sid_gets_added_note():
    """确有注入的 sid（injected[sid] 非空）→ 才给"依赖已补"提示（真前提）。"""
    granted = {"st-x": "mod/pom.xml"}
    g = _dep_recovery_retry_guidance(granted, {}, injected={"st-x": ["totp"]})
    assert "依赖已补" in g["st-x"]


# ── D2/D3 guidance replace 语义（复核 MEDIUM/F4 整改锁） ──
def test_merge_guidance_replaces_stale_d2_line_not_accumulate():
    """★复核 MEDIUM 锁★ 缺包集跨轮变化 → 旧 D2 包列表【被替换】而非堆叠；A4 诊断保留；铁律不重复。"""
    a4 = "上次尝试的确定性判死依据（机读）：某编译错"
    r1 = _merge_dep_guidance_lines(
        a4, "\n".join([_D3_POM_IRON_LAW, _D2_UNPROVISIONED_TMPL.format(pkgs=["pkg.a"])]))
    assert a4 in r1 and "pkg.a" in r1
    # 第二轮：worker 修了 pkg.a、暴露 pkg.b —— 陈旧 pkg.a 行必须消失
    r2 = _merge_dep_guidance_lines(
        r1, "\n".join([_D3_POM_IRON_LAW, _D2_UNPROVISIONED_TMPL.format(pkgs=["pkg.b"])]))
    assert "pkg.b" in r2, f"新包列表应在；实得: {r2}"
    assert "pkg.a" not in r2, f"陈旧 D2 包列表应被替换非堆叠；实得: {r2}"
    assert a4 in r2, "A4 诊断行不该被误剔"
    assert r2.count("【pom 铁律】") == 1, "D3 铁律不得跨轮堆叠"
