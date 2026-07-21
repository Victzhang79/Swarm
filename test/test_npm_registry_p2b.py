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


def test_resolve_explicit_range_respected(monkeypatch):
    def boom(url):
        raise AssertionError("显式 range 无需查 registry")

    monkeypatch.setattr(nr, "_http_get", boom)
    kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert dropped == []
    assert kept[0].name == "axios" and kept[0].spec == "^1.6.0" and kept[0].source == "explicit"


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
