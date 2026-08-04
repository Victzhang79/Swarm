"""P-H4b：cargo_registry 确定性解析 + cargo 脚手架 driver 接线。

锁的命题（每条都要答「哪个突变更让它红」）：
  · crate 名归一化（磁盘 [package].name 优先，greenfield 归一化标签只是约定）；
  · cargo semver 有界求值：bare=caret（★与 npm 正面冲突，不复用 npm 求值器★）、
    0.x 特例、tilde/精确/通配/复合、预发布只在同核心预发布界下参与、yanked 剔除；
  · 显式 req=主张非证据（P-C2 平移）：`*`→解析具体版、协议/乱写不判原样保留、
    不可达 fail-open、不可满足→校正最新稳定版/如实丢弃；
  · 工程清单中间证据层（零网络）：字符串/表形态/workspace=true 反解/根优先/
    失败可辨（硬检查④）；Cargo.lock「曾经装上」证据层；features 不静默丢（extras 同律）；
  · 内部 crate 绝不送 crates.io；内部依赖【物化 path 相对引用】（与 python 不物化相反——
    cargo path 按清单目录解析=确定性）；held（无物理落点）不送 registry 不生成 path；
  · 接线：裸名在 registry 全程 boom 下仍进权威 Cargo.toml 模板 ⇒ 唯一可能是清单层到达。
"""
from __future__ import annotations

import json
import logging

import pytest

from swarm.brain import cargo_registry as cr


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """默认离线：单测绝不真联网。需要网络的用例各自打桩 _http_get。"""
    monkeypatch.setenv("SWARM_CARGO_LOOKUP", "1")  # 开关开，但 _http_get 被打桩
    monkeypatch.setattr(cr, "_http_get", lambda url: None)
    # 缓存跨用例污染防护（与 dep_http_cache 的 TTL 层平行——测试间必须互不可见）
    cr._http_cache.clear()
    cr._http_neg_until.clear()
    yield
    cr._http_cache.clear()
    cr._http_neg_until.clear()


def _crate_doc(*versions: str, max_stable: str | None = None,
               yanked: tuple[str, ...] = ()) -> str:
    return json.dumps({
        "crate": {"name": "x", "max_stable_version": max_stable or (versions[-1] if versions else None)},
        "versions": [{"num": v, "yanked": v in yanked} for v in versions]})


# ── crate 名归一化与契约串解析 ────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("Auth", "auth"), ("auth service", "auth-service"), ("web_api", "web_api"),
    ("My Crate!", "my-crate"), ("tokio", "tokio"),
])
def test_normalize_crate_name_forms(raw, expect):
    assert cr.normalize_crate_name(raw) == expect


@pytest.mark.parametrize("raw,name,feats,req", [
    ("tokio", "tokio", (), ""),
    ("tokio@1.38", "tokio", (), "1.38"),
    ("tokio@^1.38.0", "tokio", (), "^1.38.0"),
    ("serde@>=1.0, <2", "serde", (), ">=1.0, <2"),
    # features 从宽认（声明检查从宽、坐标源从严，L10）
    ("tokio[full]@1", "tokio", ("full",), "1"),
    ("tokio[full, macros]", "tokio", ("full", "macros"), ""),
])
def test_parse_dep_text_forms(raw, name, feats, req):
    assert cr._parse_dep_text(raw) == (name, feats, req)


@pytest.mark.parametrize("raw", ["", " crates.io 裸 URL", "@1.2", "[full]"])
def test_parse_dep_text_unparseable_returns_none(raw):
    assert cr._parse_dep_text(raw) is None


# ── cargo semver 有界求值（★bare=caret，与 npm 精确语义正面冲突★） ────

@pytest.mark.parametrize("req,kind", [
    ("1.2.3", "simple"), ("^1.2", "simple"), ("~1", "simple"), ("=1.2.3", "simple"),
    (">=1.2, <2", "simple"), ("1.2.*", "simple"), ("1.*", "simple"),
    (">=1.0.0-alpha", "simple"),
    ("*", "wildcard_any"),
    ("git:https://x/y", "protocol"),
    ("1.2 || 2.0", "complex"),          # cargo 无 ||——落「不判」档而非误判
    ("1.2.3.*", "complex"),             # 通配只能占次版本/补丁位——非法形态不判
    ("nonsense", "complex"), (">=abc", "complex"),
])
def test_range_kind_classification(req, kind):
    assert cr._range_kind(req) == kind


@pytest.mark.parametrize("req,vers,expect", [
    # bare=caret：1.2.3 兼容 1.9.0 但不放 2.0.0（npm 精确语义下 1.9.0 不满足——分野锁）
    ("1.2.3", {"1.2.3", "1.9.0", "2.0.0"}, True),
    ("1.2.3", {"1.2.2", "2.0.0"}, False),
    # 0.x 特例：^0.2.3 = >=0.2.3 <0.3.0（最左非零段在已声明段位里找）
    ("0.2.3", {"0.2.9"}, True), ("0.2.3", {"0.3.0"}, False),
    ("0.0.3", {"0.0.4"}, False), ("0.0.3", {"0.0.3"}, True),
    ("0.0", {"0.0.9"}, True), ("0.0", {"0.1.0"}, False),
    # 部分段：1.2 = >=1.2.0 <2.0.0
    ("1.2", {"1.9.9"}, True), ("1.2", {"1.1.9"}, False),
    # tilde：~1.2.3 = >=1.2.3 <1.3.0
    ("~1.2.3", {"1.2.9"}, True), ("~1.2.3", {"1.3.0"}, False),
    ("~1", {"1.9.9"}, True), ("~1", {"2.0.0"}, False),
    # 精确（部分段=前缀区间）
    ("=1.2.3", {"1.2.3"}, True), ("=1.2.3", {"1.2.4"}, False),
    ("=1.2", {"1.2.9"}, True), ("=1.2", {"1.3.0"}, False),
    # 通配
    ("1.2.*", {"1.2.9"}, True), ("1.2.*", {"1.3.0"}, False),
    # 复合 AND
    (">=1.2, <2", {"1.5.0"}, True), (">=1.2, <2", {"2.0.0"}, False),
    (">=1.2, <2", {"1.1.0"}, False),
    # 预发布：无预发布界 → 预发布版本不参与（`>=1.0` 不放进 1.5.0-rc.1）
    (">=1.0", {"1.5.0-rc.1"}, False),
    # 预发布界 → 同核心预发布参与（`>=1.5.0-alpha` 放进 1.5.0-beta）
    (">=1.5.0-alpha", {"1.5.0-beta"}, True),
    # 但预发布界不放进【其它核心】的预发布
    (">=1.5.0-alpha", {"1.6.0-rc.1"}, False),
    # ★cr R1 #1★ 预发布段必须参与比较（只看数字三元组两个方向都错）：
    # `>1.5.0-alpha` 必须放进同核心更高的 beta（三元组相同，alpha<beta）
    (">1.5.0-alpha", {"1.5.0-beta"}, True),
    # `>=1.5.0-beta` 绝不能放进更低的 alpha
    (">=1.5.0-beta", {"1.5.0-alpha"}, False),
    # `<=1.5.0-alpha` 绝不能放进更高的 beta
    ("<=1.5.0-alpha", {"1.5.0-beta"}, False),
    # `=1.2.3-alpha` 只匹配 alpha（beta/正式版都不匹配）
    ("=1.2.3-alpha", {"1.2.3-beta"}, False), ("=1.2.3-alpha", {"1.2.3"}, False),
    ("=1.2.3-alpha", {"1.2.3-alpha"}, True),
    # caret 预发布下界：`^1.2.3-alpha` 放进同核心 beta 与正式版 1.2.3，不放 1.2.2
    ("^1.2.3-alpha", {"1.2.3-beta"}, True), ("^1.2.3-alpha", {"1.2.3"}, True),
    ("^1.2.3-alpha", {"1.2.2"}, False),
])
def test_range_is_satisfiable_cargo_semantics(req, vers, expect):
    assert cr._range_is_satisfiable(req, frozenset(vers)) is expect


def test_registry_versions_excludes_yanked(monkeypatch):
    """yanked 对新需求不可选（cargo 只给已在 lock 里的放行）→ 判「可用」必须剔除；
    全 yanked → 空集=「存在但零可用」与「不可达(None)」机读可辨。"""
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "1.1.0", yanked=("1.1.0",)))
    assert cr.registry_versions("x") == frozenset({"1.0.0"})
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", yanked=("1.0.0",)))
    assert cr.registry_versions("x") == frozenset()
    monkeypatch.setattr(cr, "_http_get", lambda url: None)
    assert cr.registry_versions("x") is None


def test_registry_latest_uses_max_stable_field(monkeypatch):
    """最新稳定版取权威字段 max_stable_version（不做本地 semver 排序——预发布口径
    以 registry 为准）。"""
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "2.0.0-rc.1", max_stable="1.0.0"))
    assert cr.registry_latest_version("x") == "1.0.0"
    # 零稳定版（crate 只有预发布）→ None → 调用方如实丢弃
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: json.dumps({"crate": {"max_stable_version": None},
                                                "versions": [{"num": "1.0.0-alpha",
                                                              "yanked": False}]}))
    assert cr.registry_latest_version("x") is None


# ── 工程清单中间证据层（零网络） ──────────────────────────────────────

def test_manifest_specs_string_table_forms(tmp_path):
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\n[dependencies]\n'
        'serde = "1.0"\n'
        'tokio = { version = "1.38", features = ["full", "macros"] }\n'
        'internal-x = { path = "../internal-x" }\n'
        'git-dep = { git = "https://x/y" }\n')
    specs = cr.project_manifest_specs(str(tmp_path))
    assert specs["serde"] == ((), False, "1.0")
    assert specs["tokio"] == (("full", "macros"), False, "1.38")   # features 保留（静默丢=换语义）
    assert "internal-x" not in specs    # path 表不是 crates.io 版本主张
    assert "git-dep" not in specs       # git 表同


def test_manifest_specs_workspace_inheritance_via_root_channel(tmp_path):
    """workspace 继承的**唯一**证据通道=根 [workspace.dependencies] 直接收集
    （harness P-H4b-j 实证：成员侧 workspace=true 反解臂是冗余防御——根先收让反解
    结果在 specs 里永不可达，已删）。成员 `{workspace=true}` 条目【跳过但不毒化】
    整表（L10 从宽）；无基座的 workspace=true 诚实缺席。"""
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["api"]\n[workspace.dependencies]\n'
        'serde = { version = "1.0", features = ["derive"] }\n')
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "Cargo.toml").write_text(
        '[package]\nname = "api"\n[dependencies]\n'
        'serde = { workspace = true, features = ["rc"] }\n'
        'anyhow = { workspace = true }\n')     # 根没声明 anyhow → 诚实缺席
    specs = cr.project_manifest_specs(str(tmp_path))
    assert specs["serde"] == (("derive",), False, "1.0")
    assert "anyhow" not in specs


def test_manifest_specs_member_plain_deps_read(tmp_path):
    """成员 Cargo.toml 的**普通声明**必须到达（根先收只在根自己也声明时才盖过成员；
    成员读取被删 → reqwest 缺席 → 本条红）。"""
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["api"]\n[workspace.dependencies]\n'
        'anyhow = "1.0"\n')
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "Cargo.toml").write_text(
        '[package]\nname = "api"\n[dependencies]\n'
        'anyhow = { workspace = true }\nreqwest = "0.12"\n')
    specs = cr.project_manifest_specs(str(tmp_path))
    assert specs["anyhow"] == ((), False, "1.0")
    assert specs["reqwest"] == ((), False, "0.12")


def test_manifest_specs_member_declaration_beats_workspace_default(tmp_path):
    """★cr R1 #2★ 真实声明 > 继承默认值：成员自己的 `serde = "1.0"` 必须盖过
    workspace 默认 `serde = "2.0"`（倒置=给成员写它没声明过的大版本）。"""
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["api"]\n[workspace.dependencies]\nserde = "2.0"\n')
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "Cargo.toml").write_text(
        '[package]\nname = "api"\n[dependencies]\nserde = "1.0"\n')
    specs = cr.project_manifest_specs(str(tmp_path))
    assert specs["serde"] == ((), False, "1.0")


def test_manifest_specs_conflict_warns_on_differing_declaration(tmp_path, caplog):
    """★hunter R1 F-4★ 同名不同声明被盖必须留痕（硬检查④：确定性选择≠静默选择）；
    且子目录按名序先见先收（a/ 赢 z/，与 iterdir 原生顺序无关）。"""
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["a", "z"]\n')
    for d, v in (("z", "=1.0.200"), ("a", "=1.0.100")):
        (tmp_path / d).mkdir()
        (tmp_path / d / "Cargo.toml").write_text(
            f'[package]\nname = "{d}"\n[dependencies]\nserde = "{v}"\n')
    with caplog.at_level(logging.WARNING, logger="swarm.brain.cargo_registry"):
        specs = cr.project_manifest_specs(str(tmp_path))
    assert specs["serde"] == ((), False, "=1.0.100"), "名字序先见先收（确定性）"
    assert any("冲突" in r.getMessage() and "serde" in r.getMessage()
               for r in caplog.records), "不同声明被盖零信号=降级无痕"


def test_manifest_specs_default_features_preserved(tmp_path):
    """★hunter R1 F-2★ `default-features = false` 必须保留——静默丢=重新打开默认特性
    （编译产物/传递依赖全变=换语义）。"""
    (tmp_path / "Cargo.toml").write_text(
        '[dependencies]\ntokio = { version = "1", default-features = false, '
        'features = ["rt"] }\n')
    specs = cr.project_manifest_specs(str(tmp_path))
    assert specs["tokio"] == (("rt",), True, "1")


def test_manifest_specs_malformed_toml_warns_not_silent(tmp_path, caplog):
    """硬检查④：解析失败 ≠ 没有声明（层内自吞=外层永远收不到）。"""
    (tmp_path / "Cargo.toml").write_text("[dependencies\nbroken = =")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.cargo_registry"):
        specs = cr.project_manifest_specs(str(tmp_path))
    assert specs == {}
    assert any("解析失败" in r.getMessage() for r in caplog.records)


def test_manifest_specs_gated_by_lookup(monkeypatch, tmp_path):
    """开关契约「关闭后=解析不到」——本地证据层同受其约束（与 npm/pypi 同形）。"""
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nserde = "1.0"\n')
    monkeypatch.setenv("SWARM_CARGO_LOOKUP", "0")
    assert cr.project_manifest_specs(str(tmp_path)) == {}
    assert cr.cargo_lock_versions(str(tmp_path)) == {}


def test_cargo_lock_evidence_layer(tmp_path):
    (tmp_path / "Cargo.lock").write_text(
        '[[package]]\nname = "demo"\nversion = "0.1.0"\n\n'
        '[[package]]\nname = "serde"\nversion = "1.0.218"\n')
    assert cr.cargo_lock_versions(str(tmp_path)) == {"demo": "0.1.0", "serde": "1.0.218"}
    # lock 缺失=如实 {}（本就可选）；解析失败=WARNING+{}（坏了≠没有）
    assert cr.cargo_lock_versions(str(tmp_path / "nowhere")) == {}


def test_cargo_lock_multiversion_picks_highest_stable(tmp_path, caplog):
    """★hunter R1 F-1★ lock 多版本共存（常态：bitflags 1.x+2.x）→ 取最高稳定版
    （确定性与文件顺序无关）+ WARNING；静默取先见者=把 lock 排序巧合当语义。"""
    (tmp_path / "Cargo.lock").write_text(
        '[[package]]\nname = "bitflags"\nversion = "1.3.2"\n\n'
        '[[package]]\nname = "bitflags"\nversion = "2.6.0"\n')
    with caplog.at_level(logging.WARNING, logger="swarm.brain.cargo_registry"):
        vers = cr.cargo_lock_versions(str(tmp_path))
    assert vers == {"bitflags": "2.6.0"}
    assert any("多版本" in r.getMessage() or "版本共存" in r.getMessage()
               for r in caplog.records)


def test_cargo_lock_multiversion_all_prerelease_gives_up(tmp_path, caplog):
    """多版本且全是预发布 → 放弃该条 lock 证据（fail-honest，交下游 registry），
    绝不猜一个预发布写进权威模板。"""
    (tmp_path / "Cargo.lock").write_text(
        '[[package]]\nname = "x"\nversion = "1.0.0-alpha"\n\n'
        '[[package]]\nname = "x"\nversion = "1.0.0-beta"\n')
    with caplog.at_level(logging.WARNING, logger="swarm.brain.cargo_registry"):
        assert cr.cargo_lock_versions(str(tmp_path)) == {}
    assert any("预发布" in r.getMessage() for r in caplog.records)


# ── resolve：显式 req = 主张非证据（P-C2 平移） ───────────────────────

def test_resolve_explicit_pin_verified(monkeypatch):
    monkeypatch.setattr(cr, "_http_get", lambda url: _crate_doc("1.2.3", "1.9.0"))
    kept, internal, dropped = cr.resolve_cargo_deps(["tokio@1.2.3"])
    assert [(k.name, k.spec, k.source, k.verified) for k in kept] == [
        ("tokio", "1.2.3", "explicit", "verified")]
    assert internal == [] and dropped == []


def test_resolve_explicit_unsatisfiable_corrects_to_latest(monkeypatch, caplog):
    """区间确证不可满足（版本集含预发布、剔除 yanked）→ 校正到最新稳定版（npm 臂同律），
    绝不把幻觉区间烤进权威模板。"""
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "1.4.0", max_stable="1.4.0"))
    with caplog.at_level(logging.WARNING, logger="swarm.brain.cargo_registry"):
        kept, _, dropped = cr.resolve_cargo_deps(["tokio@^99.0.0"])
    assert [(k.name, k.spec, k.source) for k in kept] == [("tokio", "1.4.0", "registry")]
    assert dropped == []
    assert any("校正到最新稳定版" in r.getMessage() for r in caplog.records)


def test_resolve_explicit_unsatisfiable_no_stable_dropped(monkeypatch):
    """不可满足且无可用稳定版 → 如实丢弃（绝不逼 worker 编版本）。"""
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: json.dumps({"crate": {"max_stable_version": None},
                                                "versions": [{"num": "0.1.0-alpha",
                                                              "yanked": False}]}))
    kept, _, dropped = cr.resolve_cargo_deps(["tokio@^99.0.0"])
    assert kept == [] and dropped == ["tokio@^99.0.0"]


def test_resolve_explicit_unreachable_failopen_kept(monkeypatch):
    """registry 不可达 → fail-open 保留（证据缺失≠否定证据——离线跑一次就清空所有
    显式依赖=比原 bug 更坏），verified=registry_unreachable 供 dep_versions_unverified 账收编。"""
    monkeypatch.setattr(cr, "_http_get", lambda url: None)
    kept, _, dropped = cr.resolve_cargo_deps(["tokio@^99.0.0"])
    assert [(k.name, k.spec, k.verified) for k in kept] == [
        ("tokio", "^99.0.0", "registry_unreachable")]
    assert dropped == []


def test_resolve_explicit_prerelease_bound_kept(monkeypatch):
    """`>=1.5.0-alpha` 对只有 1.5.0-beta 的真 crate 不误杀（全量集含预发布 +
    同核心预发布界语义）——只对稳定版集判会冤杀（P-C2「版本集含预发布」同律）。"""
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.5.0-beta", max_stable=""))
    kept, _, dropped = cr.resolve_cargo_deps(["x@>=1.5.0-alpha"])
    assert [(k.name, k.spec) for k in kept] == [("x", ">=1.5.0-alpha")]
    assert dropped == []


def test_resolve_wildcard_star_resolves_concrete(monkeypatch, caplog):
    """`*`=语法合法但不可复现（npm dist-tag 同型）→ 解析成具体最新稳定版。"""
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "2.1.0", max_stable="2.1.0"))
    with caplog.at_level(logging.WARNING, logger="swarm.brain.cargo_registry"):
        kept, _, _ = cr.resolve_cargo_deps(["tokio@*"])
    assert [(k.name, k.spec, k.source) for k in kept] == [("tokio", "2.1.0", "registry")]
    assert any("不可复现" in r.getMessage() for r in caplog.records)


def test_resolve_protocol_and_complex_unjudgeable_kept(monkeypatch):
    """协议/来源串与超集语法 → 不判原样保留（猜语义误杀比放过幻觉更坏），
    verified=unjudgeable 机读可辨。"""
    def boom(url):
        raise AssertionError("不判形态不该咨询 registry")
    monkeypatch.setattr(cr, "_http_get", boom)
    kept, _, dropped = cr.resolve_cargo_deps(["x@git:https://x/y", "y@1 || 2"])
    assert [(k.name, k.verified) for k in kept] == [("x", "unjudgeable"), ("y", "unjudgeable")]
    assert dropped == []


# ── resolve：裸名判定序（清单 → lock → registry） ────────────────────

def test_resolve_bare_manifest_wins_zero_network(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text('[dependencies]\nserde = "1.0"\n')
    def boom(url):
        raise AssertionError("清单已答 ⇒ crates.io 不该被咨询")
    monkeypatch.setattr(cr, "_http_get", boom)
    kept, _, dropped = cr.resolve_cargo_deps(["serde"], project_path=str(tmp_path))
    assert [(k.name, k.spec, k.source) for k in kept] == [("serde", "1.0", "project_manifest")]
    assert dropped == []


def test_resolve_bare_manifest_features_merged(tmp_path, monkeypatch):
    """契约裸名+features、清单已声明：契约 features 优先，清单 features 兜底（不静默丢）。"""
    (tmp_path / "Cargo.toml").write_text(
        '[dependencies]\ntokio = { version = "1.38", features = ["rt"] }\n')
    monkeypatch.setattr(cr, "_http_get", lambda url: None)
    kept, _, _ = cr.resolve_cargo_deps(["tokio[full]"], project_path=str(tmp_path))
    assert kept[0].features == ("full",)
    kept2, _, _ = cr.resolve_cargo_deps(["tokio"], project_path=str(tmp_path))
    assert kept2[0].features == ("rt",)


def test_resolve_bare_lock_then_registry(tmp_path, monkeypatch):
    (tmp_path / "Cargo.lock").write_text('[[package]]\nname = "serde"\nversion = "1.0.218"\n')
    def boom(url):
        raise AssertionError("lock 已答 ⇒ crates.io 不该被咨询")
    monkeypatch.setattr(cr, "_http_get", boom)
    kept, _, _ = cr.resolve_cargo_deps(["serde"], project_path=str(tmp_path))
    assert [(k.name, k.spec, k.source) for k in kept] == [("serde", "1.0.218", "cargo_lock")]
    # lock 没有 → registry 最新稳定版写字面量（bare=caret 语义）
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "2.1.0", max_stable="2.1.0"))
    kept2, _, _ = cr.resolve_cargo_deps(["tokio"], project_path=str(tmp_path))
    assert [(k.name, k.spec, k.source) for k in kept2] == [("tokio", "2.1.0", "registry")]


def test_resolve_bare_unresolvable_dropped(monkeypatch):
    monkeypatch.setattr(cr, "_http_get", lambda url: None)
    kept, _, dropped = cr.resolve_cargo_deps(["ghost-crate"])
    assert kept == [] and dropped == ["ghost-crate"]


def test_resolve_internal_never_hits_registry(monkeypatch):
    """内部 crate（含归一化标签）绝不送 crates.io——同名公网 crate 会被误物化
    （cr#2/hunter#1 同律）。"""
    def boom(url):
        raise AssertionError("内部 crate 被送去 crates.io 了")
    monkeypatch.setattr(cr, "_http_get", boom)
    kept, internal, dropped = cr.resolve_cargo_deps(
        ["my-auth", "auth-service"], internal_modules={"my-auth", "auth-service"})
    assert kept == [] and sorted(internal) == ["auth-service", "my-auth"] and dropped == []


# ── 模板渲染（TOML 转义，P-H4a cr R3 同律） ──────────────────────────

def test_render_cargo_toml_escapes_and_roundtrips():
    """渲染产物必须能被 tomllib 真解析且值逐字相等（非子串探针）；特殊字符名/req
    不转义=权威模板产出非法 TOML。"""
    import tomllib
    from swarm.brain.contract_utils import _render_cargo_toml
    dep = cr.ResolvedCargoDep(name="serde", spec='>=1.0, <2', source="registry")
    dep_feat = cr.ResolvedCargoDep(name="tokio", spec="1.38", source="registry",
                                   features=("full", 'quo"te'))
    text = _render_cargo_toml("my-api", [dep, dep_feat], [("my-core", "../my-core")],
                              edition="2021")
    data = tomllib.loads(text)
    assert data["package"]["name"] == "my-api"
    assert data["package"]["edition"] == "2021"
    assert data["dependencies"]["serde"] == ">=1.0, <2"
    assert data["dependencies"]["tokio"] == {"version": "1.38",
                                             "features": ["full", 'quo"te']}
    assert data["dependencies"]["my-core"] == {"path": "../my-core"}


def test_render_cargo_toml_omits_edition_without_evidence():
    """edition 缺席=省略该字段（血规 2：工具链版本绝不猜——cargo 默认 2015 会让 async
    代码在 L1 响亮编译失败，不是静默漂移）。"""
    from swarm.brain.contract_utils import _render_cargo_toml
    text = _render_cargo_toml("x", [], [])
    assert "edition" not in text


def test_render_cargo_toml_default_features_false_roundtrips():
    """★hunter R1 F-2★ default_features=False 必须渲染 `default-features = false` 且
    tomllib round-trip 逐字相等（静默丢=重新打开默认特性=换语义）；裸字符串形态只在
    【无 features 且默认特性开】时启用。"""
    import tomllib
    from swarm.brain.contract_utils import _render_cargo_toml
    dep = cr.ResolvedCargoDep(name="tokio", spec="1", source="project_manifest",
                              features=("rt",), default_features=False)
    dep_plain = cr.ResolvedCargoDep(name="serde", spec="1.0", source="registry")
    text = _render_cargo_toml("x", [dep, dep_plain], [])
    data = tomllib.loads(text)
    assert data["dependencies"]["tokio"] == {"version": "1", "default-features": False,
                                             "features": ["rt"]}
    assert data["dependencies"]["serde"] == "1.0", "无 features 且默认开 → 裸字符串形态"


def test_resolve_bare_manifest_default_features_carried(tmp_path, monkeypatch):
    """★hunter R1 F-2★ resolve 清单臂把 `default-features = false` 带到 ResolvedCargoDep
    （渲染靠它决定表形态——链路断了渲染测试再绿也是假绿）。"""
    (tmp_path / "Cargo.toml").write_text(
        '[dependencies]\ntokio = { version = "1", default-features = false, '
        'features = ["rt"] }\n')
    def boom(url):
        raise AssertionError("清单已答 ⇒ crates.io 不该被咨询")
    monkeypatch.setattr(cr, "_http_get", boom)
    kept, _, _ = cr.resolve_cargo_deps(["tokio"], project_path=str(tmp_path))
    assert kept[0].default_features is False
    assert kept[0].features == ("rt",)


def test_cargo_crate_name_warns_on_malformed_manifest(tmp_path, caplog):
    """★hunter R1 F-3★ 清单存在却解析失败 → crate 名降级归一化标签【必须 WARNING】
    （静默降级=path 依赖名与磁盘真名错位而零信号，cargo metadata 才炸）。"""
    from swarm.brain.contract_utils import _cargo_crate_name
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "Cargo.toml").write_text("[package\nname = = broken")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        name = _cargo_crate_name(str(tmp_path), "api", "My Api")
    assert name == "my-api", "解析失败 → 归一化标签兜底"
    assert any("解析失败" in r.getMessage() or "读取/解析失败" in r.getMessage()
               for r in caplog.records)


def test_cargo_root_edition_warns_on_malformed_root(tmp_path, caplog):
    """★hunter R1 F-3★ 根清单解析失败 → edition 证据缺席【必须 WARNING】
    （「证据坏了」与「真没声明」不可分=降级无痕，硬检查④）。"""
    from swarm.brain.contract_utils import _cargo_root_edition
    (tmp_path / "Cargo.toml").write_text("[workspace\nbroken = =")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        assert _cargo_root_edition(str(tmp_path)) == ""
    assert any("edition" in r.getMessage() for r in caplog.records)


# ── driver 接线锁（经真实调用链） ─────────────────────────────────────

def _cargo_plan(*, artifacts=("serde",), modules=("api",), dep_on=None):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="task st-1",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["crates/api/src/lib.rs"]))],
        parallel_groups=[["st-1"]])
    deps = [{"module": m, "artifacts": list(artifacts) if m == "api" else []}
            for m in modules]
    plan.shared_contract = {"dependencies": deps}
    return plan


def test_ph4b_cargo_manifest_spec_reaches_scaffold_via_real_caller(tmp_path, monkeypatch):
    """★接线锁（血规 10 第一条）★ registry 全程 boom；裸名仍进权威 Cargo.toml 模板 ⇒
    唯一可能是工程清单层经真实调用链到达。把 driver 的 `project_path=project_path`
    改成 None → 本条必红（裸名被 drop ⇒ 模板里没有 serde 行）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/api"]\n[workspace.dependencies]\n'
        'serde = { version = "1.0", features = ["derive"] }\n')
    def boom(url):
        raise AssertionError("清单已答 ⇒ crates.io 不该被咨询（接线断了才会走到这里）")
    monkeypatch.setattr(cr, "_http_get", boom)
    plan = _cargo_plan(artifacts=("serde",))
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected, "夹具没走到 cargo driver（零注入 ⇒ 这条测试什么也没证明）"
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert 'serde = { version = "1.0", features = ["derive"] }' in scaffold.description, \
        "清单声明（含 features）没进权威 Cargo.toml 模板 ⇒ P-H4b 证据层在生产链路上是断的"
    assert 'name = "api"' in scaffold.description
    # edition 不猜锁：根清单没声明 ⇒ 模板【省略】该字段（血规 2）
    assert "edition" not in scaffold.description


def test_ph4b_cargo_internal_dep_materialized_as_path(tmp_path, monkeypatch):
    """内部 crate【物化 path 相对引用】（与 python 不物化相反——cargo path 按清单目录
    解析=确定性）；且绝不送 crates.io。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    def boom(url):
        raise AssertionError("内部 crate 被送去 crates.io 了")
    monkeypatch.setattr(cr, "_http_get", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["crates/api/src/lib.rs"])),
                  SubTask(id="st-2", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["crates/core/src/lib.rs"]))],
        parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "api", "artifacts": ["core"]},
        {"module": "core", "artifacts": []}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert injected, "夹具没走到 cargo driver"
    api_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    # 内部依赖物化：path 相对引用（api 视角看 core）
    assert 'core = { path = "../core" }' in api_scaffold.description
    # 契约里保留（同源剪后内部仍在）
    api_entry = next(e for e in plan.shared_contract["dependencies"] if e["module"] == "api")
    assert "core" in api_entry["artifacts"]
    # 验收措辞含 cargo metadata 闸
    assert any("cargo metadata" in ac for ac in api_scaffold.acceptance_criteria)


def test_ph4b_cargo_label_vs_disk_name_internal_stays_internal(tmp_path, monkeypatch):
    """契约 artifact 写【模块标签】而磁盘 [package].name 不同（标签 `core` vs 磁盘名
    `my-core-lib`）：内部判定必须认归一化标签（否则送 crates.io 误解析同名公网 crate），
    且 path 依赖名写磁盘真名。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    (tmp_path / "crates" / "core").mkdir(parents=True)
    (tmp_path / "crates" / "core" / "Cargo.toml").write_text(
        '[package]\nname = "my-core-lib"\nversion = "0.1.0"\n')
    def boom(url):
        raise AssertionError("内部 crate（标签形）被送去 crates.io 了")
    monkeypatch.setattr(cr, "_http_get", boom)
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["crates/api/src/lib.rs"])),
                  SubTask(id="st-2", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[], create_files=["crates/core/src/lib.rs"]))],
        parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "api", "artifacts": ["core"]},
        {"module": "core", "artifacts": []}]}
    inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    api_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert 'my-core-lib = { path = "../core" }' in api_scaffold.description


def test_ph4b_cargo_unresolved_internal_label_held_from_registry(tmp_path, monkeypatch, caplog):
    """★hunter R2 H-1 同律★ 契约声明了但解析不出物理落点的模块：held 扣下——不送
    crates.io、不生成 path（无落点=臆造路径），留契约 + WARNING 机读可辨。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/api"]\n')
    hits: list[str] = []
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: hits.append(url) or _crate_doc("1.0.0"))
    plan = _cargo_plan(artifacts=("ghost",))
    # ghost 在契约标签里（另一模块的 module 键）但解析不出物理落点
    plan.shared_contract["dependencies"].append({"module": "ghost", "artifacts": []})
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    api_scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert "ghost" not in api_scaffold.description or "path" not in api_scaffold.description
    assert not any("ghost" in u for u in hits), "held 模块被送去 crates.io 了"
    assert any("#31-P2e" in r.getMessage() and "无物理落点" in r.getMessage()
               for r in caplog.records)


def test_ph4b_cargo_explicit_hallucinated_version_never_in_template(tmp_path, monkeypatch):
    """P-C2 平移的生产链路锁：`serde@^99.0` 未经核验烤进权威模板=派 worker 去失败；
    校正/丢弃后模板里绝不能有 99。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/api"]\n')
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "1.0.218", max_stable="1.0.218"))
    plan = _cargo_plan(artifacts=("serde@^99.0",))
    inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-api")
    assert "99.0" not in scaffold.description
    assert 'serde = "1.0.218"' in scaffold.description


def test_ph4b_cargo_owner_backfill_path(tmp_path, monkeypatch):
    """cr#1 同律：Cargo.toml 已被 owner 子任务认领 → backfill 嵌块（绝不静默留它手写），
    不另立 scaffold 子任务。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/api"]\n')
    monkeypatch.setattr(cr, "_http_get",
                        lambda url: _crate_doc("1.0.0", "1.0.218", max_stable="1.0.218"))
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan
    plan = TaskPlan(
        subtasks=[SubTask(id="st-1", description="task st-1",
                          difficulty=SubTaskDifficulty.MEDIUM,
                          scope=FileScope(writable=[],
                                          create_files=["crates/api/Cargo.toml",
                                                        "crates/api/src/lib.rs"]))],
        parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [{"module": "api", "artifacts": ["serde"]}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None)
    assert not any(st.id == "st-scaffold-api" for st in plan.subtasks), \
        "owner 已认领就该 backfill，不该另立 scaffold"
    st1 = next(st for st in plan.subtasks if st.id == "st-1")
    assert 'serde = "1.0.218"' in st1.description, "确定性清单块没嵌进 owner description"
