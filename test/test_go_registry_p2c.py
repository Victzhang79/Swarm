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
    gr._probe_cache.clear()   # P-C2 新缓存：不清会把上一条的探测三态漏给下一条
    yield
    gr._http_cache.clear()
    gr._probe_cache.clear()


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


def test_resolve_explicit_version_kept_after_verification(monkeypatch):
    """显式版本**经 proxy 证实存在**后原样保留（版本与 source 都不动）。

    ★契约已随 P-C2 变更★ 本测试原名 `..._respected`、前提是"显式版本无需查 proxy，直采"，
    那正是 P-C2（27 号文 §3.1）作废的东西：显式版本是**待验证的主张**，绝非证据（R67L-B3
    口径平移）。此处保住的残值是"证实之后不篡改"——只验证、不多事。
    误杀/幻觉/不可达三个方向在 test_pc2_explicit_version_is_a_claim.py 里。
    """
    monkeypatch.setattr(gr, "_http_probe", lambda url: True)

    def boom(url):
        raise AssertionError("存在性已由 probe 答出，不该再走 /@latest 文本取值")

    monkeypatch.setattr(gr, "_http_get", boom)
    kept, internal, dropped = gr.resolve_go_deps(["github.com/x/y@v1.2.3"])
    assert kept[0].module == "github.com/x/y" and kept[0].version == "v1.2.3"
    assert kept[0].source == "explicit"
    assert dropped == []


def test_resolve_explicit_version_never_probes_when_lookup_disabled(monkeypatch):
    """开关关闭 = 全线不联网，显式版本 fail-open 原样保留（离线绝不批量误杀）。"""
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")

    def boom(*a, **kw):
        raise AssertionError("SWARM_GO_LOOKUP=0 时绝不联网")

    monkeypatch.setattr(gr.urllib.request, "urlopen", boom)
    kept, internal, dropped = gr.resolve_go_deps(["github.com/x/y@v1.2.3"])
    assert kept[0].version == "v1.2.3" and kept[0].source == "explicit"
    assert dropped == []


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


# ══════════════════════════════════════════════════════════════════
# P-H3（27 号文）：工程**自身** go.mod 的 require 钉版 = 裸 module 的零网络证据层
# ══════════════════════════════════════════════════════════════════
# 治前裸 module 只认 `$GOPATH/pkg/mod`（已下载）与 proxy（联网）——E2E 沙箱是新
# clone（cache 空）、proxy 一抖，契约依赖被如实丢弃，而工程自己 go.mod 的 require 行
# 就是钉死的可解析版本（比 proxy 最新更贴合本工程）。
# 判定序：本地 module cache → 工程自身 go.mod 钉版（本层）→ proxy。


def test_ph3_go_mod_requires_parses_both_require_forms(tmp_path):
    """取证面：单行 + 块两种 require 形态、`// indirect` 行、根优先、replace 行不收。"""
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.22\n\n"
        "require github.com/gin-gonic/gin v1.9.1\n\n"
        "require (\n"
        "\tgithub.com/golang-jwt/jwt/v5 v5.2.0\n"
        "\tgolang.org/x/text v0.14.0 // indirect\n"
        ") // 块结束也允许带注释（R2-2：不识它会把后续 exclude 块当 require 收）\n\n"
        "require\tgithub.com/tabby/dep v1.0.0\n"     # R2-1：tab 分隔是合法形态
        "require  github.com/spacy/dep v2.0.0\n"    # R2-1：多空格也是
        "exclude (\n"
        "\tgithub.com/bad2/pkg v3.0.0\n"
        ")\n\n"
        "exclude (\n"
        "\tgithub.com/bad/pkg v1.0.0\n"
        ")\n\n"
        "replace example.com/old v1.0.0 => ../old\n\n"
        "retract v2.0.0\n", encoding="utf-8")
    sub = tmp_path / "svc"   # 单层子目录
    sub.mkdir()
    (sub / "go.mod").write_text(
        "module example.com/app/svc\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v9.9.9\n",
        encoding="utf-8")
    pins = gr.project_go_mod_requires(str(tmp_path))
    assert pins["github.com/gin-gonic/gin"] == "v1.9.1", "根优先：子目录同名钉版不得覆盖根"
    assert pins["github.com/golang-jwt/jwt/v5"] == "v5.2.0"
    assert pins["golang.org/x/text"] == "v0.14.0", "// indirect 行同样收录（钉版是真实证据）"
    assert "example.com/old" not in pins, "replace 行不是 require 钉版（`=>` 尾必须挡住）"
    assert "github.com/bad/pkg" not in pins, \
        "★复核 R1-1★ exclude 块内行是【被排除的坏版本】，剥前缀法会把它冒充钉版——必须整段跳过"
    assert "github.com/bad2/pkg" not in pins, \
        "★复核 R2-2★ `) // 注释` 也是合法块结束——不识它，后续 exclude 块会被当 require 收"
    assert "retract" not in pins, "★复核 R1-1★ retract 单行是【下架声明】，不是 require 钉版"
    assert pins["github.com/tabby/dep"] == "v1.0.0", "★复核 R2-1★ tab 分隔的单行 require 必须收"
    assert pins["github.com/spacy/dep"] == "v2.0.0", "★复核 R2-1★ 多空格分隔的单行 require 必须收"


def test_ph3_go_mod_layer_respects_lookup_switch(tmp_path, monkeypatch):
    """★消费契约★ 开关文档口径=「关闭后=解析不到→如实丢弃」（`local_module_cache_version`
    同契约）。本层不门控就会在离线模式里静默破约。"""
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        encoding="utf-8")
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")
    assert gr.project_go_mod_requires(str(tmp_path)) == {}
    kept, _, dropped = gr.resolve_go_deps(["github.com/gin-gonic/gin"],
                                          project_path=str(tmp_path))
    assert kept == [] and dropped == ["github.com/gin-gonic/gin"]


def test_ph3_bare_module_resolves_from_go_mod_pin(tmp_path, monkeypatch):
    """裸 module + 工程 go.mod 有钉版（cache 空）→ 采用钉版，proxy 零咨询。"""
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        encoding="utf-8")

    def boom(mod):
        raise AssertionError("go.mod 已答 ⇒ proxy 不该被咨询")

    monkeypatch.setattr(gr, "proxy_latest_version", boom)
    kept, _, dropped = gr.resolve_go_deps(["github.com/gin-gonic/gin"],
                                          project_path=str(tmp_path))
    assert dropped == []
    assert [(k.module, k.version, k.source, k.verified) for k in kept] == \
        [("github.com/gin-gonic/gin", "v1.9.1", "go_mod", "verified")]


def test_ph3_local_cache_beats_go_mod_pin(tmp_path, monkeypatch):
    """★判定序锁★ 本地 module cache（已下载=最强证据）> go.mod 钉版。"""
    gopath = tmp_path / "gopath"
    cache = gopath / "pkg" / "mod" / "github.com" / "gin-gonic"
    cache.mkdir(parents=True)
    (cache / "gin@v1.8.0").mkdir()
    monkeypatch.setenv("GOPATH", str(gopath))
    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        encoding="utf-8")

    def boom(mod):
        raise AssertionError("cache 已答 ⇒ proxy 不该被咨询")

    monkeypatch.setattr(gr, "proxy_latest_version", boom)
    kept, _, _ = gr.resolve_go_deps(["github.com/gin-gonic/gin"], project_path=str(tmp_path))
    assert [(k.version, k.source) for k in kept] == [("v1.8.0", "local")]


def test_ph3_go_mod_miss_falls_through_to_proxy(tmp_path, monkeypatch):
    """go.mod 没钉 ⇒ 照旧落 proxy：本层只加证据，不改既有出口。"""
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    monkeypatch.setattr(gr, "_http_get", lambda url: _latest("v1.10.0"))
    kept, _, dropped = gr.resolve_go_deps(["github.com/gin-gonic/gin"],
                                          project_path=str(tmp_path))
    assert dropped == []
    assert [(k.version, k.source) for k in kept] == [("v1.10.0", "proxy")]


def test_ph3_go_mod_miss_and_proxy_dead_still_drops(tmp_path, monkeypatch):
    """fail-honest 方向不变：go.mod 无、proxy 查无 ⇒ 如实丢弃（血规 2，绝不兜底造假）。"""
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    monkeypatch.setattr(gr, "_http_get", lambda url: None)
    kept, _, dropped = gr.resolve_go_deps(["example.com/ghost"], project_path=str(tmp_path))
    assert kept == [] and dropped == ["example.com/ghost"]


def test_ph3_go_mod_enum_oserror_warns_and_degrades(tmp_path, caplog):
    """★复核 R2-1★ 枚举失败（目录不可读）≠「真没有子目录 go.mod」——必须 WARNING 可辨。"""
    import logging
    import os

    (tmp_path / "go.mod").write_text(
        "module example.com/app\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
        encoding="utf-8")
    sub = tmp_path / "svc"
    sub.mkdir()
    (sub / "go.mod").write_text("module example.com/app/svc\n", encoding="utf-8")
    os.chmod(tmp_path, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            pins = gr.project_go_mod_requires(str(tmp_path))
    finally:
        os.chmod(tmp_path, 0o755)
    assert pins == {}
    assert any("枚举失败" in r.getMessage() for r in caplog.records), \
        "枚举异常被层内自吞 ⇒ 「目录坏了」与「真没有」不可分"


def test_ph3_go_mod_read_oserror_warns_and_skips(tmp_path, caplog):
    """★复核 R2-1★ go.mod 存在但读不出 ≠「没有 require」——必须 WARNING 可辨（硬检查④）。"""
    import logging
    import os

    gm = tmp_path / "go.mod"
    gm.write_text("module example.com/app\n\ngo 1.22\n\nrequire github.com/gin-gonic/gin v1.9.1\n",
                  encoding="utf-8")
    os.chmod(gm, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            pins = gr.project_go_mod_requires(str(tmp_path))
    finally:
        os.chmod(gm, 0o644)
    assert pins == {}, "读不出还采到了钉版 ⇒ 夹具没压到目标分支"
    assert any("读取失败" in r.getMessage() for r in caplog.records), \
        "读取异常被层内自吞 ⇒ 「文件坏了」与「真没有 require」不可分"
