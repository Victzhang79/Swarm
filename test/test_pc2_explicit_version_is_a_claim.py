"""P-C2（27 号文 §3.1）：npm/go 的显式版本是**待验证的主张，绝非证据**。

治前：`npm_registry` / `go_registry` 对 `name@range` 一律"契约已给定，尊重之"直采，零验证
⇒ `axios@^99.0.0` / `lodash@nonsense` 原样烤进**权威 package.json 模板**要 worker"原样
写入"，而模板即真值 worker 无权改 → `npm install` 整包装不上。**规划期自己在猜坐标 = 正面
违反血规 2**。Maven 侧 R67L-B3 早已定论此事，本批把口径平移过来。

★本文件的重心是"误杀方向"★ 判幻觉的能力天然带来批量误杀真依赖的风险：只要把"registry
不可达"错当成"版本不存在"，离线跑一次就会清空所有显式依赖。故 R56-6（证据缺失≠否定证据）
在这里是**硬约束**，多条用例专门锁它。
"""
from __future__ import annotations

import logging
import urllib.error

import pytest

from swarm.brain import go_registry as gr
from swarm.brain import npm_registry as nr


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """默认禁联网（血规：绝不让测试依赖网络，也杜绝"网络好就绿、离线就红"的假绿）。
    需要"registry 可达"的用例各自 monkeypatch 具体取证函数。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "0")
    monkeypatch.setenv("SWARM_GO_LOOKUP", "0")
    nr._http_cache.clear()
    gr._http_cache.clear()
    gr._probe_cache.clear()


# ══════════════════════════════════════════════════════════════════
# ① npm：区间可满足性语义（纯函数，零网络）
# ══════════════════════════════════════════════════════════════════

_PUBLISHED = frozenset({"1.6.0", "1.6.7", "1.7.2", "2.0.0",
                        "0.2.3", "0.2.9", "0.0.3", "1.8.0-beta.1"})


@pytest.mark.parametrize("spec,expect", [
    # 精确：写全 x.y.z 必须真有该版本
    ("1.6.0", True), ("1.6.1", False), ("=1.7.2", True), ("v2.0.0", True),
    # 缺位 = 前缀区间
    ("1.6", True), ("1.9", False), ("1", True), ("3", False),
    # caret：同 major 且 ≥ floor
    ("^1.6.0", True), ("^1.9.0", False), ("^99.0.0", False), ("^2.0.0", True),
    # ★caret 的 major-0 特例★ `^0.2.3` = `>=0.2.3 <0.3.0`（semver：0.x 次版本即破坏性）
    ("^0.2.3", True), ("^0.3.0", False),
    # `^0.0.z` 更严：精确
    ("^0.0.3", True), ("^0.0.4", False),
    # tilde：同 major.minor 且 ≥ floor
    ("~1.6.0", True), ("~1.9.0", False), ("~0.2.3", True),
    # 比较运算
    (">=1.0.0", True), (">=99.0.0", False), (">2.0.0", False), (">1.9.0", True),
    # 预发布版本存在过 → 不判幻觉（我们不主动注入预发布是**另一档**决定）
    ("1.8.0-beta.1", True),
])
def test_range_satisfiability_semantics(spec, expect):
    """逐条 parametrize（而非一个 for 循环）＝每条语义单独承重。

    某条判错的后果：过宽 → 放过幻觉（治理失效）；过窄 → **误杀真依赖**（比治前更坏）。
    故两个方向都有用例，尤其 caret 的 major-0 特例（最容易写错的一格）。
    """
    assert nr._range_is_satisfiable(spec, _PUBLISHED) is expect


@pytest.mark.parametrize("spec,kind", [
    ("^1.6.0", "simple"), ("1.6.0", "simple"), (">=1.0.0", "simple"),
    # 协议/别名：不是版本主张（Maven `${...}` 的对应物）
    ("workspace:*", "protocol"), ("file:../shared", "protocol"),
    ("link:../x", "protocol"), ("git+https://g/x.git", "protocol"),
    ("https://x/y.tgz", "protocol"), ("npm:other-pkg@1.0.0", "protocol"),
    ("github:user/repo", "protocol"), ("portal:../p", "protocol"),
    # dist-tag / 通配：语法合法但**不可复现**
    ("latest", "dist_tag"), ("*", "dist_tag"), ("next", "dist_tag"), ("", "dist_tag"),
    # 复合区间：无 semver 库判不准 → 刻意不判（猜语义会误杀）
    (">=1.0.0 <2.0.0", "complex"), ("1.0.0 || 2.0.0", "complex"),
    ("1.2.3 - 2.0.0", "complex"), ("nonsense", "complex"), ("1.x", "complex"),
])
def test_range_kind_classification(spec, kind):
    """可判性分类。★分错的后果不对称★ 把协议错判成 simple → 误杀（`workspace:*` 会被
    当版本区间去比对，必然不可满足）；把 simple 错判成 complex → 只是放过（治理变弱）。
    """
    assert nr._range_kind(spec) == kind


# ══════════════════════════════════════════════════════════════════
# ② npm：接线 —— 幻觉被校正 / 不可达被 fail-open
# ══════════════════════════════════════════════════════════════════

def test_npm_hallucinated_range_is_corrected_to_latest(monkeypatch, caplog):
    """★病灶本体★ `axios@^99.0.0` 必须**不再**原样保留。

    治前：直采 → 烤进权威 package.json 模板 → `npm install` 整包失败。
    治后：registry 确证无任何版本可满足 → 校正到最新稳定版（与 Maven 侧 R67L-B3 同处置）。
    """
    monkeypatch.setattr(nr, "registry_all_versions", lambda pkg: _PUBLISHED)
    monkeypatch.setattr(nr, "registry_latest_version", lambda pkg, pp=None: "2.0.0")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.npm_registry"):
        kept, dropped = nr.resolve_npm_deps(None, ["axios@^99.0.0"])
    assert dropped == []
    assert [(d.name, d.spec, d.source) for d in kept] == [("axios", "^2.0.0", "registry")]
    assert [r for r in caplog.records if "P-C2" in r.getMessage()], "校正无留痕"


def test_npm_hallucinated_range_with_no_stable_is_dropped_honestly(monkeypatch, caplog):
    """确证幻觉 + 无可用稳定版 → **如实丢弃**（血规 2 的 fail-honest），绝不逼 worker 臆造。

    dropped 必须回传，调用方据此**同时从验收剔除**——否则又造出"模板没有、验收却要求"的矛盾。
    """
    monkeypatch.setattr(nr, "registry_all_versions", lambda pkg: _PUBLISHED)
    monkeypatch.setattr(nr, "registry_latest_version", lambda pkg, pp=None: None)
    with caplog.at_level(logging.WARNING, logger="swarm.brain.npm_registry"):
        kept, dropped = nr.resolve_npm_deps(None, ["ghost-pkg@^99.0.0"])
    assert kept == []
    assert dropped == ["ghost-pkg@^99.0.0"]
    assert [r for r in caplog.records if "如实丢弃" in r.getMessage()]


def test_npm_unreachable_registry_fails_open_and_keeps_the_claim(monkeypatch, caplog):
    """★★误杀防线（R56-6：证据缺失≠否定证据）★★ registry 不可达时**保留** LLM 主张。

    这是本批最危险的方向：若把"不可达"当成"版本不存在"，**离线跑一次就清空所有显式依赖**，
    比治前（放过幻觉）坏得多。`registry_all_versions` 返 None 即不可达，必须 fail-open + 留痕。
    """
    monkeypatch.setattr(nr, "registry_all_versions", lambda pkg: None)
    with caplog.at_level(logging.WARNING, logger="swarm.brain.npm_registry"):
        kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert dropped == []
    assert [(d.name, d.spec, d.source) for d in kept] == [("axios", "^1.6.0", "explicit")]
    assert [r for r in caplog.records if "未经证实" in r.getMessage()], \
        "fail-open 降级无留痕（血规 3）"


def test_npm_satisfiable_range_is_kept_verbatim(monkeypatch):
    """真实可满足的区间原样保留——治理**不得**顺手改写合法声明（那是另一种误杀）。"""
    monkeypatch.setattr(nr, "registry_all_versions", lambda pkg: _PUBLISHED)
    kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert dropped == []
    assert [(d.name, d.spec, d.source) for d in kept] == [("axios", "^1.6.0", "explicit")]


@pytest.mark.parametrize("spec", ["workspace:*", "file:../shared", "git+https://g/x.git",
                                  ">=1.0.0 <2.0.0", "1.0.0 || 2.0.0"])
def test_npm_unjudgeable_forms_are_never_touched(monkeypatch, spec):
    """协议/别名/复合区间**绝不判定**（Maven `${...}` 的对应物）。

    逐条 parametrize：任一形态漏进判定分支都会被拿去比对版本集 → 必然"不可满足" → 误杀。
    同时锁住"不判时不查网"——`registry_all_versions` 被换成会炸的实现。
    """
    def _boom(pkg):
        raise AssertionError(f"不该为 {spec} 查 registry（不判形态）")
    monkeypatch.setattr(nr, "registry_all_versions", _boom)
    kept, dropped = nr.resolve_npm_deps(None, [f"pkg-x@{spec}"])
    assert dropped == []
    assert [(d.name, d.spec) for d in kept] == [("pkg-x", spec)]


def test_npm_dist_tag_latest_is_resolved_not_kept(monkeypatch):
    """`latest` 语法合法但**不可复现**——模块 docstring 明列它为要治的病之一，故解析成具体版本。"""
    monkeypatch.setattr(nr, "registry_all_versions", lambda pkg: _PUBLISHED)
    monkeypatch.setattr(nr, "registry_latest_version", lambda pkg, pp=None: "2.0.0")
    kept, _ = nr.resolve_npm_deps(None, ["axios@latest"])
    assert [(d.name, d.spec) for d in kept] == [("axios", "^2.0.0")]


def test_npm_workspace_internal_still_wins_over_version_judgement(monkeypatch):
    """内部 workspace 包的分流**在**版本判定之前——它们根本不在 registry 上，查必然查不到。

    顺序错的后果：内部包被判"幻觉"→ 校正成某个同名公网包的版本，或被丢弃 → monorepo 崩。
    """
    def _boom(pkg):
        raise AssertionError("内部包不该查 registry")
    monkeypatch.setattr(nr, "registry_all_versions", _boom)
    kept, dropped = nr.resolve_npm_deps(None, ["@app/shared@^1.0.0"],
                                        internal_names={"@app/shared"})
    assert dropped == []
    assert [(d.name, d.spec, d.source) for d in kept] == \
        [("@app/shared", "workspace:*", "workspace")]


def test_registry_all_versions_includes_prereleases(monkeypatch):
    """★与 `registry_latest_version` 刻意分档★ 本函数答"存在过吗"必须含预发布；
    那个答"该写哪个"只要稳定版。混用会把 `1.8.0-beta.1` 判成幻觉（误杀）。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    monkeypatch.setattr(nr, "_http_get", lambda url:
                        '{"versions":{"1.0.0":{},"2.0.0-rc.1":{}}}')
    assert nr.registry_all_versions("x") == frozenset({"1.0.0", "2.0.0-rc.1"})


def test_registry_all_versions_empty_or_broken_doc_is_none_not_empty(monkeypatch):
    """★空返回必须机读可辨（血规 10 第四条）★ 文档取到但 `versions` 空/畸形 → 归一成 None
    （＝不可达），**绝不**返空集合。

    返空集合的后果：调用方拿它去判可满足性 → 恒 False → 把该包所有版本判成幻觉（误杀本体）。
    """
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    for doc in ('{"versions":{}}', '{"versions":null}', '{}', '{"versions":[]}', 'not-json'):
        nr._http_cache.clear()
        monkeypatch.setattr(nr, "_http_get", lambda url, _d=doc: _d)
        assert nr.registry_all_versions("x") is None, f"{doc} 应归一为 None"


# ══════════════════════════════════════════════════════════════════
# ③ go：三态探测 + 接线
# ══════════════════════════════════════════════════════════════════

def _fake_urlopen(status=None, http_code=None, exc=None, body='{"Version":"v1.2.3"}'):
    class _Resp:
        def __init__(self, s):
            self.status = s

        def read(self):
            return body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        if exc is not None:
            raise exc
        if http_code is not None:
            raise urllib.error.HTTPError(req.full_url, http_code, "x", {}, None)
        return _Resp(status)
    return _open


@pytest.mark.parametrize("kw,expect", [
    ({"status": 200}, True),
    ({"http_code": 404}, False),        # 确证不存在
    ({"http_code": 410}, False),        # 同上（gone）
    ({"http_code": 500}, None),         # 服务端故障 ≠ 不存在
    ({"http_code": 429}, None),         # 限流 ≠ 不存在
    ({"exc": urllib.error.URLError("offline")}, None),
    ({"exc": TimeoutError("slow")}, None),
    ({"exc": OSError("dns")}, None),
])
def test_http_probe_is_tristate(monkeypatch, kw, expect):
    """★三态是硬要求★ 只有 404/410 才算"确证不存在"；其余一律 None（不可达）。

    治前 `_http_get` 把 404 与离线**都返 None** ⇒ 拿它判存在性，离线一次批量误杀。
    ★HTTPError 是 URLError 的子类★ except 顺序写反会把 404 也吞成 None（治理整体失效，
    且无声）——本测试的 404/410 两格就是那个顺序的锁。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    monkeypatch.setattr(gr.urllib.request, "urlopen", _fake_urlopen(**kw))
    gr._probe_cache.clear()
    assert gr._http_probe("https://x/y.info") is expect


def test_proxy_version_exists_requires_all_mirrors_to_confirm_absence(monkeypatch):
    """★镜像语义★ 任一镜像不可达 ⇒ None（**不是** False）。

    "官方 404 + 镜像超时"这种组合在国内网络是常态。若据此判 False，就会把真实存在但官方
    暂时抽风的版本判成幻觉 → 误杀。只有**全部**镜像都答 404 才算确证。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    seq = iter([False, None])            # 第一个镜像确证无、第二个不可达
    monkeypatch.setattr(gr, "_http_probe", lambda url: next(seq))
    assert gr.proxy_version_exists("github.com/x/y", "v1.0.0") is None

    monkeypatch.setattr(gr, "_http_probe", lambda url: False)
    assert gr.proxy_version_exists("github.com/x/y", "v1.0.0") is False

    monkeypatch.setattr(gr, "_http_probe", lambda url: True)
    assert gr.proxy_version_exists("github.com/x/y", "v1.0.0") is True


def test_proxy_version_exists_with_no_mirrors_is_none_not_false(monkeypatch):
    """★零镜像 = 零证据 → None，绝不 False★ 单独立一条，因为它是**唯一**能触发末行
    `return False if saw_false else None` 里 `else` 分支的入口。

    上一条（镜像语义）无法区分这一行：`[False, None]` 那一路在循环里就早返了，把末行改成
    裸 `return False` 它照样绿——两处防御互相兜底 ⇒ 任一处单独突变都不可证伪
    （[[swarm-redundant-defense-unfalsifiable]]）。命题本身是真的不变量：镜像表被配空
    （或将来改成从配置读）时，"一个都没问到"必须是"未证实"，不能塌成"确证不存在"。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")

    def _boom(url):
        raise AssertionError("镜像表为空时不该发起任何探测")

    monkeypatch.setattr(gr, "_PROXY_INFO_MIRRORS", ())
    monkeypatch.setattr(gr, "_http_probe", _boom)
    assert gr.proxy_version_exists("github.com/x/y", "v1.0.0") is None


def test_go_hallucinated_version_is_corrected(monkeypatch, caplog):
    """★病灶本体（go 侧）★ proxy 确证查无 → 校正到 `/@latest`，绝不原样烤进 go.mod 模板。"""
    monkeypatch.setattr(gr, "proxy_version_exists", lambda m, v: False)
    monkeypatch.setattr(gr, "proxy_latest_version", lambda m: "v1.9.1")
    with caplog.at_level(logging.WARNING, logger="swarm.brain.go_registry"):
        kept, internal, dropped = gr.resolve_go_deps(["github.com/gin-gonic/gin@v99.0.0"])
    assert (internal, dropped) == ([], [])
    assert [(d.module, d.version, d.source) for d in kept] == \
        [("github.com/gin-gonic/gin", "v1.9.1", "proxy")]
    assert [r for r in caplog.records if "P-C2" in r.getMessage()]


def test_go_hallucinated_version_no_latest_is_dropped(monkeypatch):
    """确证查无 + 无可用版本 → 如实丢弃（调用方须同时从验收剔除）。"""
    monkeypatch.setattr(gr, "proxy_version_exists", lambda m, v: False)
    monkeypatch.setattr(gr, "proxy_latest_version", lambda m: None)
    kept, _, dropped = gr.resolve_go_deps(["example.com/ghost@v99.0.0"])
    assert kept == []
    assert dropped == ["example.com/ghost@v99.0.0"]


def test_go_unreachable_proxy_fails_open(monkeypatch, caplog):
    """★★误杀防线（R56-6）★★ proxy 不可达 → 保留 LLM 主张 + 留痕。"""
    monkeypatch.setattr(gr, "proxy_version_exists", lambda m, v: None)
    with caplog.at_level(logging.WARNING, logger="swarm.brain.go_registry"):
        kept, _, dropped = gr.resolve_go_deps(["github.com/x/y@v1.2.3"])
    assert dropped == []
    assert [(d.module, d.version, d.source) for d in kept] == \
        [("github.com/x/y", "v1.2.3", "explicit")]
    assert [r for r in caplog.records if "未经证实" in r.getMessage()]


@pytest.mark.parametrize("ver", [
    # ★伪版本两种形态都要认★ 只认下面第二种（`_PSEUDO` 的窄口径）会漏掉**更常见**的第一种。
    # 那条窄模式的老消费者 `_is_stable` 靠"主体含 `-` 即非稳定"兜住了第二形态，所以窄口径在
    # 那边不是 bug；本档判漏的后果相反 = 误杀（血规 10 第三条）。
    "v0.0.0-20230101120000-abcdef123456",     # 无前置 tag（最常见）
    "v1.2.3-0.20230101120000-abcdef123456",   # 有前置 tag
    "latest", "master", "main",              # 分支名/别名
    "abcdef1234567890",                      # 裸 commit SHA
    "v1.2",                                  # 非规范（go.mod 要三段）
])
def test_go_unjudgeable_versions_are_never_probed(monkeypatch, ver):
    """非规范 semver tag **绝不判定**（Maven `${...}` 的对应物）。

    逐条 parametrize + 把探测换成会炸的实现＝同时锁"不判时不查网"。伪版本尤其重要：
    它是真实可用的形态，判它必然 404 → 误杀。
    """
    def _boom(m, v):
        raise AssertionError(f"不该为 {ver} 探测 proxy（不判形态）")
    monkeypatch.setattr(gr, "proxy_version_exists", _boom)
    kept, _, dropped = gr.resolve_go_deps([f"github.com/x/y@{ver}"])
    assert dropped == []
    assert [(d.module, d.version) for d in kept] == [("github.com/x/y", ver)]


def test_go_incompatible_suffix_is_judgeable(monkeypatch):
    """`+incompatible` 是**真实已发布**形态（v2+ 未用 module 后缀的库），必须仍可判定。

    误把它划进"不判"的后果：这类库的幻觉版本从此免检（治理漏一大片 Go 生态）。
    """
    seen = []
    monkeypatch.setattr(gr, "proxy_version_exists",
                        lambda m, v: (seen.append(v), True)[1])
    kept, _, _ = gr.resolve_go_deps(["github.com/x/y@v3.1.0+incompatible"])
    assert seen == ["v3.1.0+incompatible"], "该形态被误划进不判分支"
    assert [d.version for d in kept] == ["v3.1.0+incompatible"]


def test_go_internal_module_still_wins_over_version_judgement(monkeypatch):
    """内部 module 分流**在**判定之前（它们没发布，探测必然 404 → 会被误杀）。"""
    def _boom(m, v):
        raise AssertionError("内部 module 不该探测 proxy")
    monkeypatch.setattr(gr, "proxy_version_exists", _boom)
    kept, internal, dropped = gr.resolve_go_deps(
        ["example.com/app/auth@v1.0.0"], internal_modules={"example.com/app/auth"})
    assert (kept, dropped) == ([], [])
    assert internal == ["example.com/app/auth"]


def test_go_probe_cache_is_separate_from_text_cache(monkeypatch):
    """★两个缓存必须分开★ 值域不同（三态 `bool|None` vs `文本|None`）。

    混用一个 dict 的后果：`_http_get` 取到空文本存 None，`_http_probe` 读到同键的 None
    会当"不可达"（或反过来把 404 的 False 当文本用）——两种缺席又塌成一个值。
    """
    assert gr._http_cache is not gr._probe_cache
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    gr._probe_cache.clear()
    gr._http_cache.clear()

    # ★关键夹具形状★ 让**同一个 URL** 在文本缓存里先留下一个 None（`_http_get` 取到空/失败
    # 的正常记法）。若探测去读文本缓存，就会把这个 None 当成"不可达"直接返回——而真相是
    # 404（确证不存在）。仅断言"两个 dict 不同一"或"探测没污染文本缓存"都抓不到这个方向：
    # 混用只需改**读**，写还落在自己那边，两个 dict 依然是不同对象、文本缓存依然是空的。
    gr._http_cache["https://x/y.info"] = None
    monkeypatch.setattr(gr.urllib.request, "urlopen", _fake_urlopen(http_code=404))
    assert gr._http_probe("https://x/y.info") is False, \
        "探测读了文本缓存的 None → 把 404 确证退化成不可达（两种缺席塌成一个值）"
    assert gr._http_cache["https://x/y.info"] is None, "探测覆写了文本缓存"

    # 反向：探测结果不得被 `_http_get` 读成文本。
    gr._probe_cache.clear()
    gr._http_cache.clear()
    gr._probe_cache["probe::https://x/z.info"] = False
    monkeypatch.setattr(gr.urllib.request, "urlopen",
                        _fake_urlopen(status=200, body='{"Version":"v1.0.0"}'))
    assert gr._http_get("https://x/z.info") == '{"Version":"v1.0.0"}'
