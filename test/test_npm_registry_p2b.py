"""#31-Phase2b：npm 版本确定性解析器单测（离线确定性 + 网络路径打桩）。

纪律（同 maven_registry）：SWARM_NPM_LOOKUP=0 时全线不联网、解析不到即丢弃（fail-honest）；
网络路径用 monkeypatch 打桩 _http_get，绝不真联网（杜绝"网络好就绿、离线就红"的假绿）。
红线复核点：R12 绝不臆造版本（查不到必 drop，绝不 `latest`/编造）；内部 workspace 包
绝不去 registry 查（workspace:*）。
"""
from __future__ import annotations

import json

import pytest

from swarm.brain import npm_registry as nr


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认离线：单测绝不真联网。需要网络的用例各自打桩 _http_get。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")  # 开关开，但 _http_get 被打桩
    nr._http_cache.clear()
    yield
    nr._http_cache.clear()


def _mk_registry_doc(latest=None, versions=None):
    doc = {}
    if latest is not None:
        doc["dist-tags"] = {"latest": latest}
    if versions is not None:
        doc["versions"] = {v: {} for v in versions}
    return json.dumps(doc)


# ── 稳定版判定 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("v,stable", [
    ("1.2.3", True), ("0.0.1", True), ("10.20.30", True),
    ("1.2.3-beta.1", False), ("2.0.0-rc.0", False), ("1.0.0-next.5", False),
    ("1.0.0-alpha", False), ("3.0.0-canary.20240101", False),
    ("1.2.3+build.7", True),  # build 元数据不算预发布
])
def test_is_stable(v, stable):
    assert nr._is_stable(v) is stable


def test_ver_key_orders_semver():
    assert nr._ver_key("1.10.0") > nr._ver_key("1.9.9")
    assert nr._ver_key("2.0.0") > nr._ver_key("1.99.99")


# ── name@range 拆分（含 scoped 包）──────────────────────────────────────────
@pytest.mark.parametrize("raw,name,rng", [
    ("axios", "axios", None),
    ("axios@^1.6.0", "axios", "^1.6.0"),
    ("@scope/pkg", "@scope/pkg", None),
    ("@scope/pkg@1.2.3", "@scope/pkg", "1.2.3"),
    ("@babel/core@^7.0.0", "@babel/core", "^7.0.0"),
])
def test_split_name_range(raw, name, rng):
    assert nr._split_name_range(raw) == (name, rng)


# ── 版本解析（网络打桩）────────────────────────────────────────────────────
def test_registry_latest_prefers_dist_tags(monkeypatch):
    monkeypatch.setattr(nr, "_http_get", lambda url: _mk_registry_doc(latest="1.6.8"))
    assert nr.registry_latest_version("axios") == "1.6.8"


def test_registry_latest_filters_prerelease_dist_tag(monkeypatch):
    """脏 latest 指向预发布 → 回退全量 versions 最大稳定版（防御 R12：绝不采预发布）。"""
    monkeypatch.setattr(nr, "_http_get",
                        lambda url: _mk_registry_doc(latest="2.0.0-rc.1",
                                                     versions=["1.9.0", "1.10.2", "2.0.0-rc.1"]))
    assert nr.registry_latest_version("pkg") == "1.10.2"


def test_registry_latest_none_when_only_prerelease(monkeypatch):
    """全是预发布 → None（绝不硬塞一个预发布版）。"""
    monkeypatch.setattr(nr, "_http_get",
                        lambda url: _mk_registry_doc(versions=["1.0.0-alpha", "1.0.0-beta.1"]))
    assert nr.registry_latest_version("pkg") is None


def test_registry_latest_falls_back_to_mirror(monkeypatch):
    """官方查不通 → 镜像兜底。"""
    calls = []

    def fake_get(url):
        calls.append(url)
        return _mk_registry_doc(latest="3.1.0") if "npmmirror" in url else None

    monkeypatch.setattr(nr, "_http_get", fake_get)
    assert nr.registry_latest_version("lodash") == "3.1.0"
    assert any("npmmirror" in u for u in calls)


def test_registry_latest_offline_returns_none(monkeypatch):
    """SWARM_NPM_LOOKUP=0 → 不联网 → None（绝不臆造）。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "0")
    assert nr.registry_latest_version("axios") is None


def test_scoped_pkg_url_encoded(monkeypatch):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return _mk_registry_doc(latest="7.24.0")

    monkeypatch.setattr(nr, "_http_get", fake_get)
    assert nr.registry_latest_version("@babel/core") == "7.24.0"
    assert "%2F" in seen["url"].upper()  # scoped 的 / 必须转义


# ── 本地 node_modules 证据优先 ──────────────────────────────────────────────
def test_local_node_modules_version_wins(tmp_path, monkeypatch):
    nm = tmp_path / "node_modules" / "axios"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(json.dumps({"name": "axios", "version": "1.5.0"}),
                                     encoding="utf-8")
    # registry 会给更高版，但本地已装 = 确定能装的最强证据 → 采本地
    monkeypatch.setattr(nr, "_http_get", lambda url: _mk_registry_doc(latest="1.9.9"))
    assert nr.registry_latest_version("axios", str(tmp_path)) == "1.5.0"


def test_local_node_modules_prerelease_ignored(tmp_path, monkeypatch):
    nm = tmp_path / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(json.dumps({"version": "2.0.0-beta.1"}), encoding="utf-8")
    monkeypatch.setattr(nr, "_http_get", lambda url: _mk_registry_doc(latest="1.8.0"))
    # 本地是预发布 → 忽略本地 → 回退 registry 稳定版
    assert nr.registry_latest_version("pkg", str(tmp_path)) == "1.8.0"


# ── resolve_npm_deps 主入口 ─────────────────────────────────────────────────
def test_resolve_internal_workspace_never_hits_registry(monkeypatch):
    """内部 workspace 包 → workspace:* 且绝不触网（红线：兄弟包不在 registry）。"""
    def boom(url):
        raise AssertionError("内部包绝不应查 registry")

    monkeypatch.setattr(nr, "_http_get", boom)
    kept, dropped = nr.resolve_npm_deps(None, ["@app/shared"], internal_names={"@app/shared"})
    assert dropped == []
    assert len(kept) == 1
    assert kept[0].name == "@app/shared" and kept[0].spec == "workspace:*"
    assert kept[0].source == "workspace"


def test_resolve_explicit_range_kept_after_verification(monkeypatch):
    """显式 range **经 registry 证实可满足**后原样保留（range 文本与 source 都不动）。

    ★契约已随 P-C2 变更★ 本测试原名 `..._respected`、前提是"显式 range 无需查 registry，
    直采"，那正是 P-C2（27 号文 §3.1）作废的东西：显式版本是**待验证的主张**，绝非证据
    （R67L-B3 口径平移）。此处保住的残值是"证实之后不篡改"——`^1.6.0` 不会被换成 `^1.7.2`。
    误杀/幻觉/不可达三个方向在 test_pc2_explicit_version_is_a_claim.py 里。
    """
    monkeypatch.setattr(nr, "_http_get",
                        lambda url: _mk_registry_doc(latest="1.7.2",
                                                     versions=["1.5.0", "1.6.0", "1.6.8"]))
    kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert dropped == []
    assert kept[0].name == "axios" and kept[0].spec == "^1.6.0" and kept[0].source == "explicit"


def test_resolve_explicit_range_never_queries_when_lookup_disabled(monkeypatch):
    """开关关闭 = 全线不联网，显式 range fail-open 原样保留（离线绝不批量误杀）。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "0")

    def boom(*a, **kw):
        raise AssertionError("SWARM_NPM_LOOKUP=0 时绝不联网")

    monkeypatch.setattr(nr.urllib.request, "urlopen", boom)
    kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert dropped == []
    assert kept[0].spec == "^1.6.0" and kept[0].source == "explicit"


def test_resolve_bare_third_party_caret_prefixed(monkeypatch):
    monkeypatch.setattr(nr, "_http_get", lambda url: _mk_registry_doc(latest="4.18.2"))
    kept, dropped = nr.resolve_npm_deps(None, ["express"])
    assert dropped == []
    assert kept[0].name == "express" and kept[0].spec == "^4.18.2"


def test_resolve_unresolvable_dropped_never_guessed(monkeypatch):
    """R12 红线：查不到版本 → drop，绝不臆造/latest。"""
    monkeypatch.setattr(nr, "_http_get", lambda url: None)
    kept, dropped = nr.resolve_npm_deps(None, ["does-not-exist-xyz"])
    assert kept == []
    assert dropped == ["does-not-exist-xyz"]


def test_resolve_mixed_and_dedup(monkeypatch):
    def fake_get(url):
        if "axios" in url:
            return _mk_registry_doc(latest="1.6.8")
        return None  # ghost 包查不到

    monkeypatch.setattr(nr, "_http_get", fake_get)
    kept, dropped = nr.resolve_npm_deps(
        None,
        ["@app/core", "axios", "axios", "ghost-pkg"],
        internal_names={"@app/core"})
    names = [k.name for k in kept]
    assert names == ["@app/core", "axios"]  # 去重 + 保序
    assert dropped == ["ghost-pkg"]


def test_resolve_offline_all_third_party_dropped(monkeypatch):
    """离线：内部包仍 workspace:*（零网络），第三方全 drop（fail-honest）。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "0")
    kept, dropped = nr.resolve_npm_deps(None, ["@app/x", "react"], internal_names={"@app/x"})
    assert [k.name for k in kept] == ["@app/x"]
    assert dropped == ["react"]


# ══════════════════════════════════════════════════════════════════
# P-H3（27 号文）：工程**自身** package.json 声明 = 裸名解析的零网络证据层
# ══════════════════════════════════════════════════════════════════
# 治前裸名只认 node_modules（已安装）与 registry（联网）——E2E 沙箱是新 clone（无
# node_modules）、registry 一抖，契约里写着的依赖被如实丢弃，答案却写在同仓
# package.json 里（曾经装上过的声明，比 registry 最新版更贴合本工程）。
# 判定序：node_modules → 工程自身清单声明（本层）→ registry。


def test_ph3_manifest_specs_root_and_one_level(tmp_path):
    """取证面：根 + 单层子目录；node_modules 里的清单是安装产物不是工程声明；根优先。"""
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"axios": "^1.6.0", "shared": "^2.0.0"},
        "devDependencies": {"vitest": "^1.0.0"},
    }), encoding="utf-8")
    sub = tmp_path / "web"   # 单层子目录（packages/web 两层深=诚实边界外，不收录）
    sub.mkdir()
    (sub / "package.json").write_text(
        json.dumps({"dependencies": {"shared": "^9.9.9", "vue": "^3.4.0"}}), encoding="utf-8")
    # node_modules/package.json 正好被「*/package.json」glob 到——跳过谓词的承重夹具
    nm = tmp_path / "node_modules"
    nm.mkdir()
    (nm / "package.json").write_text(json.dumps({"dependencies": {"ghost": "1.0.0"}}),
                                     encoding="utf-8")
    specs = nr.project_manifest_specs(str(tmp_path))
    assert specs["axios"] == "^1.6.0" and specs["vitest"] == "^1.0.0"
    assert specs["vue"] == "^3.4.0", "单层子目录的清单也要收录"
    assert specs["shared"] == "^2.0.0", "根优先：子模块同名声明不得覆盖根"
    assert "ghost" not in specs, "node_modules 里的清单是安装产物，不是工程声明"


def test_ph3_manifest_specs_malformed_json_warns_and_skips(tmp_path, caplog):
    """残缺证据=没有证据（缺席即默认态），但必须可观测（硬检查④）——静默跳过会让
    「清单坏了」与「真没有声明」不可分。"""
    import logging

    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        specs = nr.project_manifest_specs(str(tmp_path))
    assert specs == {}
    assert any("P-H3" in r.getMessage() for r in caplog.records), \
        "清单解析失败零信号 ⇒ 这层可以死很久没人知道"


def test_ph3_manifest_specs_skips_empty_and_nonstr_specs(tmp_path):
    """非字符串/空白 spec 不是有效钉版证据（`"a": ""` 写进 package.json 必然装不上）。"""
    (tmp_path / "package.json").write_text(json.dumps({
        "dependencies": {"a": "  ", "b": 3, "c": "^1.0.0"},
        "devDependencies": ["not-a-dict"],
    }), encoding="utf-8")
    assert nr.project_manifest_specs(str(tmp_path)) == {"c": "^1.0.0"}


def test_ph3_manifest_layer_respects_lookup_switch(tmp_path, monkeypatch):
    """★消费契约★ 开关文档口径=「关闭后=解析不到→如实丢弃」（`local_node_modules_version`
    同契约）。本层不门控就会在离线模式里静默破约——离线是** operator 要零解析**的场景，
    不是「多找一条证据」的场景。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "0")
    assert nr.project_manifest_specs(str(tmp_path)) == {}
    kept, dropped = nr.resolve_npm_deps(str(tmp_path), ["axios"])
    assert kept == [] and dropped == ["axios"]


def test_ph3_bare_name_resolves_from_project_manifest(tmp_path, monkeypatch):
    """裸名 + 工程清单有声明（无 node_modules）→ 采用声明原文（不加 `^`、不改成别的
    版本），registry 零咨询。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")

    def boom(pkg, pp=None):
        raise AssertionError("清单已答 ⇒ registry 不该被咨询")

    monkeypatch.setattr(nr, "registry_latest_version", boom)
    kept, dropped = nr.resolve_npm_deps(str(tmp_path), ["axios"])
    assert dropped == []
    assert [(k.name, k.spec, k.source, k.verified) for k in kept] == \
        [("axios", "^1.6.0", "project_manifest", "verified")]


def test_ph3_node_modules_beats_manifest(tmp_path, monkeypatch):
    """★判定序锁★ node_modules（已安装=最强证据）> 工程清单声明。顺序反了会把「确定
    能装的版本」换成「声明区间」——解析能力没变，证据强度降档。"""
    nm = tmp_path / "node_modules" / "axios"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text(json.dumps({"version": "1.5.0"}), encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")

    def boom(pkg, pp=None):
        raise AssertionError("本地已答 ⇒ registry 不该被咨询")

    monkeypatch.setattr(nr, "registry_latest_version", boom)
    kept, _ = nr.resolve_npm_deps(str(tmp_path), ["axios"])
    assert [(k.spec, k.source) for k in kept] == [("^1.5.0", "local")]


def test_ph3_manifest_miss_falls_through_to_registry(tmp_path, monkeypatch):
    """清单没声明 ⇒ 照旧落 registry：本层只加证据，不改既有出口。"""
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"other": "1.0.0"}}), encoding="utf-8")
    monkeypatch.setattr(nr, "_http_get", lambda url: _mk_registry_doc(latest="4.18.2"))
    kept, dropped = nr.resolve_npm_deps(str(tmp_path), ["express"])
    assert dropped == []
    assert [(k.spec, k.source) for k in kept] == [("^4.18.2", "registry")]


def test_ph3_manifest_miss_and_registry_dead_still_drops(tmp_path, monkeypatch):
    """fail-honest 方向不变：清单无、registry 查无 ⇒ 如实丢弃——本层绝不变成兜底造假源
    （血规 2）。"""
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {}}), encoding="utf-8")
    monkeypatch.setattr(nr, "_http_get", lambda url: None)
    kept, dropped = nr.resolve_npm_deps(str(tmp_path), ["ghost-pkg"])
    assert kept == [] and dropped == ["ghost-pkg"]


def test_ph3_manifest_enum_oserror_warns_and_degrades(tmp_path, caplog):
    """★复核 R2-1★ 子目录枚举失败（目录不可读）≠「真没有子目录清单」——必须 WARNING
    可辨（硬检查④）。夹具用真 chmod 000（`Path.glob` 会静默吞 OSError，iterdir 才抛，
    这是实现选型的承重前提，不打桩 pathlib 内部）。"""
    import logging
    import os

    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")
    sub = tmp_path / "web"
    sub.mkdir()
    (sub / "package.json").write_text(
        json.dumps({"dependencies": {"vue": "^3.4.0"}}), encoding="utf-8")
    os.chmod(tmp_path, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            specs = nr.project_manifest_specs(str(tmp_path))
    finally:
        os.chmod(tmp_path, 0o755)
    assert specs == {}, "目录不可读时根清单也读不到（is_file 假阴性）⇒ 只能剩 WARNING 这一条信号"
    assert any("枚举失败" in r.getMessage() for r in caplog.records), \
        "枚举异常被层内自吞 ⇒ 「目录坏了」与「真没有子包」不可分"
def test_ph3_manifest_root_stat_eacces_branch_is_locked(monkeypatch, tmp_path, caplog):
    """根清单 is_file 的 EACCES 分支锁（v0.9.72 CI 红修复）：py<3.13 的 pathlib.is_file
    只吞 ENOENT/ENOTDIR，EACCES 照抛（3.13+ 才全吞）——chmod 夹具在 3.13+ 上走不到该
    分支 ⇒ 该分支在本地是【不可证伪的死代码】。直接 patch is_file 抛 PermissionError，
    版本无关地证「判定失败=WARNING+降级 {}」而非炸穿。"""
    import logging
    import pathlib
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"axios": "^1.6.0"}}), encoding="utf-8")

    def _boom(self, *a, **k):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(pathlib.Path, "is_file", _boom)
    with caplog.at_level(logging.WARNING):
        specs = nr.project_manifest_specs(str(tmp_path))
    assert specs == {}
    assert any("判定失败" in r.getMessage() for r in caplog.records), \
        "EACCES 判定失败必须落 WARNING——「目录坏了」与「真没有」不可分即层内自吞"
