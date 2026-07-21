"""#31-Phase2c：Go module 版本确定性解析器单测（离线确定性 + proxy 路径打桩）。

纪律（同 maven/npm registry）：SWARM_GO_LOOKUP=0 时全线不联网、解析不到即丢弃；proxy 路径
用 monkeypatch 打桩 _http_get，绝不真联网。红线复核点：R12 绝不臆造版本（查不到必 drop，
绝不 latest/伪版本）；内部 module 绝不查 proxy（走 replace）。
"""
from __future__ import annotations

import json

import pytest

from swarm.brain import go_registry as gr


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    monkeypatch.setenv("GOPATH", "/nonexistent-gopath-for-tests")  # 本地 cache 恒空
    gr._http_cache.clear()
    yield
    gr._http_cache.clear()


def _latest(ver):
    return json.dumps({"Version": ver, "Time": "2024-01-01T00:00:00Z"})


# ── 稳定版判定 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("v,stable", [
    ("v1.2.3", True), ("v0.0.1", True), ("v10.20.30", True),
    ("v1.2.3-beta.1", False), ("v2.0.0-rc.0", False), ("v1.0.0-alpha", False),
    ("v0.0.0-20240101000000-abcdef123456", False),  # 伪版本
    ("1.2.3", False),  # 缺 v 前缀
    ("v1.2.3+incompatible", True),  # +incompatible 是合法正式版
])
def test_is_stable(v, stable):
    assert gr._is_stable(v) is stable


def test_ver_key_orders():
    assert gr._ver_key("v1.10.0") > gr._ver_key("v1.9.9")
    assert gr._ver_key("v2.0.0") > gr._ver_key("v1.99.0")


def test_encode_uppercase_module():
    assert gr._encode_mod("github.com/Azure/azure-sdk") == "github.com/!azure/azure-sdk"
    assert gr._encode_mod("github.com/gin-gonic/gin") == "github.com/gin-gonic/gin"


@pytest.mark.parametrize("raw,mod,ver", [
    ("github.com/gin-gonic/gin", "github.com/gin-gonic/gin", None),
    ("github.com/gin-gonic/gin@v1.9.1", "github.com/gin-gonic/gin", "v1.9.1"),
])
def test_split_mod_version(raw, mod, ver):
    assert gr._split_mod_version(raw) == (mod, ver)


# ── 版本解析（proxy 打桩）──────────────────────────────────────────────────
def test_proxy_latest(monkeypatch):
    monkeypatch.setattr(gr, "_http_get", lambda url: _latest("v1.9.1"))
    assert gr.proxy_latest_version("github.com/gin-gonic/gin") == "v1.9.1"


def test_proxy_rejects_pseudo_version(monkeypatch):
    """proxy 对未打 tag 的 module 返回伪版本 → 拒采（不可复现）→ None。"""
    monkeypatch.setattr(gr, "_http_get",
                        lambda url: _latest("v0.0.0-20240101000000-abcdef123456"))
    assert gr.proxy_latest_version("github.com/x/untagged") is None


def test_proxy_rejects_prerelease(monkeypatch):
    monkeypatch.setattr(gr, "_http_get", lambda url: _latest("v2.0.0-rc.1"))
    assert gr.proxy_latest_version("github.com/x/y") is None


def test_proxy_falls_back_to_mirror(monkeypatch):
    calls = []

    def fake_get(url):
        calls.append(url)
        return _latest("v3.1.0") if "goproxy.cn" in url else None

    monkeypatch.setattr(gr, "_http_get", fake_get)
    assert gr.proxy_latest_version("github.com/x/y") == "v3.1.0"
    assert any("goproxy.cn" in u for u in calls)


def test_proxy_uppercase_module_encoded(monkeypatch):
    seen = {}

    def fake_get(url):
        seen["url"] = url
        return _latest("v1.0.0")

    monkeypatch.setattr(gr, "_http_get", fake_get)
    gr.proxy_latest_version("github.com/Azure/foo")
    assert "!azure" in seen["url"]


def test_offline_returns_none(monkeypatch):
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")
    assert gr.proxy_latest_version("github.com/gin-gonic/gin") is None


# ── 本地 module cache 证据优先 ──────────────────────────────────────────────
def test_local_cache_version_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GOPATH", str(tmp_path))
    cache = tmp_path / "pkg" / "mod" / "github.com" / "gin-gonic"
    cache.mkdir(parents=True)
    (cache / "gin@v1.8.0").mkdir()
    monkeypatch.setattr(gr, "_http_get", lambda url: _latest("v1.9.9"))
    # 本地已下载 v1.8.0 = 确定能拉 → 采本地（不引入未下载的 v1.9.9）
    assert gr.proxy_latest_version("github.com/gin-gonic/gin") == "v1.8.0"


def test_local_cache_uppercase_encoded(tmp_path, monkeypatch):
    monkeypatch.setenv("GOPATH", str(tmp_path))
    cache = tmp_path / "pkg" / "mod" / "github.com" / "!azure"
    cache.mkdir(parents=True)
    (cache / "foo@v2.1.0").mkdir()
    assert gr.local_module_cache_version("github.com/Azure/foo") == "v2.1.0"


# ── resolve_go_deps 主入口 ──────────────────────────────────────────────────
def test_resolve_internal_never_hits_proxy(monkeypatch):
    def boom(url):
        raise AssertionError("内部 module 绝不应查 proxy")

    monkeypatch.setattr(gr, "_http_get", boom)
    kept, internal, dropped = gr.resolve_go_deps(
        ["example.com/app/shared"], internal_modules={"example.com/app/shared"})
    assert kept == [] and dropped == []
    assert internal == ["example.com/app/shared"]


def test_resolve_explicit_version_respected(monkeypatch):
    def boom(url):
        raise AssertionError("显式版本无需查 proxy")

    monkeypatch.setattr(gr, "_http_get", boom)
    kept, internal, dropped = gr.resolve_go_deps(["github.com/x/y@v1.2.3"])
    assert kept[0].module == "github.com/x/y" and kept[0].version == "v1.2.3"
    assert kept[0].source == "explicit"


def test_resolve_bare_third_party(monkeypatch):
    monkeypatch.setattr(gr, "_http_get", lambda url: _latest("v1.9.1"))
    kept, internal, dropped = gr.resolve_go_deps(["github.com/gin-gonic/gin"])
    assert kept[0].module == "github.com/gin-gonic/gin" and kept[0].version == "v1.9.1"
    assert dropped == []


def test_resolve_unresolvable_dropped_never_guessed(monkeypatch):
    """R12 红线：查不到 → drop，绝不臆造。"""
    monkeypatch.setattr(gr, "_http_get", lambda url: None)
    kept, internal, dropped = gr.resolve_go_deps(["example.com/ghost/mod"])
    assert kept == [] and internal == []
    assert dropped == ["example.com/ghost/mod"]


def test_resolve_mixed_and_dedup(monkeypatch):
    def fake_get(url):
        return _latest("v1.9.1") if "gin" in url else None

    monkeypatch.setattr(gr, "_http_get", fake_get)
    kept, internal, dropped = gr.resolve_go_deps(
        ["example.com/app/core", "github.com/gin-gonic/gin",
         "github.com/gin-gonic/gin", "example.com/ghost"],
        internal_modules={"example.com/app/core"})
    assert internal == ["example.com/app/core"]
    assert [k.module for k in kept] == ["github.com/gin-gonic/gin"]
    assert dropped == ["example.com/ghost"]


def test_resolve_offline_third_party_dropped(monkeypatch):
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")
    kept, internal, dropped = gr.resolve_go_deps(
        ["example.com/app/x", "github.com/gin-gonic/gin"],
        internal_modules={"example.com/app/x"})
    assert internal == ["example.com/app/x"]
    assert dropped == ["github.com/gin-gonic/gin"]
