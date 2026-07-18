"""R65E-T4（round65e5 st-53-1 架构治本 D1）：A2 缺依赖自证坐标匹配【绝不把外部库误绑到内部模块】。

死因（三路取证）：st-53-1(ruoyi-framework 2FA) 编译错 `package dev.samstevens.totp.generator does
not exist`（totp 已 provision，此错是 pom 被小模型改坏后 totp jar 掉 classpath 的下游症状）。A2
`_find_maven_dep_for_pkg` 用【子串】匹配：token `generator`（totp 的子包叶）撞内部模块 artifactId
`ruoyi-generator` → 误注入 `ruoyi-framework → com.ruoyi:ruoyi-generator`（无意义、近成环、且根本不提供
totp）。治本：①去通用叶 token(generator/util/core/api…)；②匹配须锚定——包名以候选 groupId 为前缀
(Maven 惯例，org.quartz.*←org.quartz)，或辨识 token 命中 artifactId 的【整段】(okhttp3←okhttp)；
`dev.samstevens.totp.*` 永远锚不到 `com.ruoyi:ruoyi-generator`。fail-honest：锚不到→None，绝不臆造。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import swarm.brain.nodes as N


def _proj_with_internal_generator() -> str:
    """迷你 RuoYi 式工程：根 pom 声明内部模块 com.ruoyi:ruoyi-generator；ruoyi-framework 为失败模块。"""
    d = tempfile.mkdtemp()
    root = Path(d)
    (root / "pom.xml").write_text(
        "<project><dependencies>\n"
        "  <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-generator</artifactId>"
        "<version>${project.version}</version></dependency>\n"
        "  <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId>"
        "<version>${project.version}</version></dependency>\n"
        "</dependencies></project>\n")
    (root / "ruoyi-framework").mkdir()
    (root / "ruoyi-framework" / "pom.xml").write_text(
        "<project><parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version></parent>\n"
        "<artifactId>ruoyi-framework</artifactId>\n"
        "<dependencies>\n"
        "  <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId></dependency>\n"
        "</dependencies></project>\n")
    return d


def test_external_pkg_never_misbinds_to_internal_module():
    """★D1 回归锁★ `dev.samstevens.totp.generator` 绝不因 token `generator` 撞 `ruoyi-generator`
    而被误绑——外部库全仓无坐标 → 返回 None（fail-honest，不臆造、不注入无关内部模块）。"""
    d = _proj_with_internal_generator()
    dep = N._find_maven_dep_for_pkg(d, "dev.samstevens.totp.generator", "ruoyi-framework/pom.xml")
    assert dep is None, f"外部库误绑内部模块 ruoyi-generator！实得: {dep}"


def test_inject_no_op_for_unprovisioned_external():
    """端到端：定向恢复授权 ruoyi-framework/pom.xml 后，缺 dev.samstevens.totp.generator（外部未
    provision）→ 补依赖注入返回 {}（不往 framework pom 注无关 ruoyi-generator）。"""
    d = _proj_with_internal_generator()
    granted = {"st-53-1": "ruoyi-framework/pom.xml"}
    results = {"st-53-1": {"l1_details": {"build_output":
               "[ERROR] TwoFactorAuthService.java:[3,31] package dev.samstevens.totp.generator does not exist"}}}
    injected = N._inject_missing_maven_deps(d, granted, results)
    assert injected == {}, f"不该往 framework pom 注入无关内部模块；实得: {injected}"
    fw = (Path(d) / "ruoyi-framework" / "pom.xml").read_text()
    assert "ruoyi-generator" not in fw, "framework pom 被误注入 ruoyi-generator"


# ── keep-green：合法自证匹配必须仍然命中 ──
def _proj_with_sibling(gid: str, aid: str) -> str:
    d = tempfile.mkdtemp()
    root = Path(d)
    (root / "sched").mkdir()
    (root / "sched" / "pom.xml").write_text(
        f"<project><dependencies>\n  <dependency><groupId>{gid}</groupId>"
        f"<artifactId>{aid}</artifactId></dependency>\n</dependencies></project>\n")
    (root / "alarm").mkdir()
    (root / "alarm" / "pom.xml").write_text(
        "<project><dependencies>\n"
        "  <dependency><groupId>com.x</groupId><artifactId>common</artifactId></dependency>\n"
        "</dependencies></project>\n")
    return d


def test_keepgreen_groupid_prefix_anchor_quartz():
    """org.quartz.* ← 兄弟声明的 org.quartz:quartz（groupId 前缀锚 / artifactId 整段）仍命中。"""
    d = _proj_with_sibling("org.quartz", "quartz")
    dep = N._find_maven_dep_for_pkg(d, "org.quartz.JobDetail", "alarm/pom.xml")
    assert dep and "quartz" in dep.lower(), f"quartz 自证应命中；实得: {dep}"


def test_keepgreen_groupid_prefix_anchor_zxing_core():
    """com.google.zxing.core ← com.google.zxing:core：叶 token `core` 被去，但 groupId 前缀锚命中
    （证明去叶不伤 groupId 惯例库）。"""
    d = _proj_with_sibling("com.google.zxing", "core")
    dep = N._find_maven_dep_for_pkg(d, "com.google.zxing.qrcode.core", "alarm/pom.xml")
    assert dep and "zxing" in dep.lower(), f"zxing:core 应经 groupId 前缀锚命中；实得: {dep}"


def test_keepgreen_artifactid_segment_okhttp():
    """okhttp3 ← com.squareup.okhttp3:okhttp（非 groupId 前缀，靠辨识 token 命中 artifactId 整段）。"""
    d = _proj_with_sibling("com.squareup.okhttp3", "okhttp")
    dep = N._find_maven_dep_for_pkg(d, "okhttp3", "alarm/pom.xml")
    assert dep and "okhttp" in dep.lower(), f"okhttp 应经 artifactId 整段命中；实得: {dep}"


# ── 复核 HIGH 整改回归锁：段全落叶噪/通用的常见包不得被静默拒修 ──
def test_highfix_sole_leaf_token_survives_commons_io():
    """★复核 HIGH#2 锁★ org.apache.commons.io：唯一 token 'commons' 是叶噪但【去后空】→保留；
    命中兄弟 commons-io:commons-io（整段 'commons'）。旧改法会清空 toks 致静默拒修。"""
    d = _proj_with_sibling("commons-io", "commons-io")
    dep = N._find_maven_dep_for_pkg(d, "org.apache.commons.io", "alarm/pom.xml")
    assert dep and "commons-io" in dep.lower(), f"commons-io 自证应命中；实得: {dep}"


def test_highfix_groupid_prefix_anchor_reachable_empty_toks():
    """★复核 HIGH#1 锁★ javax.annotation：段全落通用/叶噪(javax 通用+annotation 叶噪，去后仅
    annotation 保留)——但即便 toks 为空，也【不得】提前 return None：anchor1(groupId 前缀 p==g)
    须能命中兄弟 javax.annotation:javax.annotation-api。旧早退会让它永不进 anchor1。"""
    d = _proj_with_sibling("javax.annotation", "javax.annotation-api")
    dep = N._find_maven_dep_for_pkg(d, "javax.annotation", "alarm/pom.xml")
    assert dep and "annotation" in dep.lower(), f"javax.annotation 应经 groupId 前缀锚命中；实得: {dep}"


def test_highfix_springframework_core_prefix_anchor():
    """★复核 HIGH#1/#2 锁★ org.springframework.core：core 叶噪，但兄弟 org.springframework:spring-core
    的 groupId 是包前缀 → anchor1 命中（不因 core 被去/早退而静默拒修）。"""
    d = _proj_with_sibling("org.springframework", "spring-core")
    dep = N._find_maven_dep_for_pkg(d, "org.springframework.core", "alarm/pom.xml")
    assert dep and "spring-core" in dep.lower(), f"spring-core 应经 groupId 前缀锚命中；实得: {dep}"
