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
    nr._probe_cache.clear()


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
    # ★R1 缺位＝通配★ 这三格对本集**有区分力**（补零口径下全判 False）：`~1` 要的是 1.x
    # 而集里只有 1.6/1.7/1.8，补零会去找 1.0.x；`^0`/`^0.0` 同理去找 0.0.0。
    ("~1", True), ("^0", True), ("^0.0", True),
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


@pytest.mark.parametrize("spec,published,expect", [
    # ── 缺位必须判"可满足"（原实现全判 False ⇒ 误杀）──────────────────────
    ("~18", ["18.3.1"], True),          # React 18 系最常见写法
    ("~5", ["5.1.0"], True),
    ("^0", ["0.21.1"], True),           # `^0` = `>=0.0.0 <1.0.0`
    ("^0.0", ["0.0.7"], True),          # `^0.0` = `<0.1.0`
    ("~0", ["0.4.2"], True),
    ("~4", ["4.18.2"], True),           # `express@~4` 的真实形态
    # ── 缺位**仍有上界**：治法不许把闸拧成恒 True ─────────────────────────
    ("~18", ["19.0.0"], False),
    ("~18", ["17.9.9"], False),
    ("~0", ["1.0.0"], False),
    ("^0", ["1.0.0"], False),           # 0.x 的 caret 绝不跨到 1.x
    ("^0.0", ["0.1.0"], False),         # `^0.0` 的上界是 0.1.0
    # ── 写全段位的既有语义不得回归（同一函数两条口径的对照组）─────────────
    ("~4.18", ["4.18.2"], True),
    ("~4.18", ["4.19.0"], False),
    ("~4.18", ["4.17.9"], False),
])
def test_r1_missing_segments_are_wildcards_not_zeros(spec, published, expect):
    """★P-C2 复核 R1★ npm semver 里缺位是**通配**（X-range），不是 0。

    原实现 `floor = (maj, mnr or 0, pat or 0)` 把补出来的 0 当成对**未声明段位**的
    **相等**约束（`~18` 要求 `cur[1] == 0`），于是 `~18` vs `18.3.1` 判 False。
    `""`/`=` 臂早就按 `pat is not None`/`mnr is not None` 分了档，`^`/`~` 漏了——
    **同一个函数里两种口径**。

    ★为什么误杀比丢弃重★ 判 False ⇒ 走"确证幻觉"分支 ⇒ 校正成 latest ⇒
    `express@~4` 被改成 `^5.1.0` ⇒ npm install 成功、worker 却按 4.x API 写码
    ＝**静默跨大版本漂移**，且校正不记账。

    ★为什么不复用 `_PUBLISHED`★ 那个共享集里有 `0.2.3`，`^0` 对它两版实现都返 True
    （区分力被集合内容吃掉）；"缺位的上界"必须用**只含越界版本**的集来证。
    逐案自带版本集 ⇒ 每格的被测命题唯一（[[swarm-fixture-shape-must-encode-time-skew]] 同理）。
    """
    assert nr._range_is_satisfiable(spec, frozenset(published)) is expect


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


@pytest.mark.parametrize("seq,expect,expect_calls", [
    # ★区分力核心★ 首个镜像不可达、第二个答 True：早返版本返 None，正确版本返 True。
    ([None, True], True, 2),
    # 首个不可达、第二个答 404：返回值两版都是 None（fail-open 一致），
    # **但调用次数不同**——早返版本只问 1 个镜像。次数断言是"冗余真的在工作"的唯一证据。
    ([None, False], None, 2),
    # 对照：第一个就答 True ⇒ 短路，第二个**不该**被问（省 I/O 是刻意的）。
    ([True, False], True, 1),
])
def test_f1_second_mirror_is_actually_queried(monkeypatch, seq, expect, expect_calls):
    """★P-C2 复核 F1★ 首个镜像不可达时，**第二个镜像必须仍被问**。

    原实现在 `got is None` 分支里 `return None` 即早返 ⇒ 多镜像冗余形同虚设，而 docstring
    承诺的"任一镜像答 True → True"是假的：goproxy.cn 抖一下，proxy.golang.org 明明能答
    True 也拿不到 ⇒ 真实存在的版本判不出来。方向没错（None 仍 fail-open），但把**冗余**
    这一维整个抹掉了。

    ★为什么原有那条测试证不了这个★ 它的夹具是 `iter([False, None])`——先确证无、后不可达。
    这个顺序下早返版本（第二轮才 return None）与修好的版本（`saw_false and not
    saw_unreachable` 不成立）**都返 None** ⇒ 零区分力，是唯一藏得住这个 bug 的顺序。
    实测两版实现在该夹具下逐字同结果。故本条把顺序翻成 `[None, True]`，
    并额外断**探测次数**（[[swarm-test-must-prove-wiring-not-correctness]]：
    断言必须能区分"哪道闸"，返回值相同时就去断可观测的副作用）。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    it, calls = iter(seq), []

    def _probe(url):
        calls.append(url)
        return next(it)

    monkeypatch.setattr(gr, "_http_probe", _probe)
    assert gr.proxy_version_exists("github.com/x/y", "v1.0.0") is expect
    assert len(calls) == expect_calls, calls
    assert len(set(calls)) == len(calls), f"同一镜像被问了两次: {calls}"


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
    # ★伪版本**三种**形态都要认（复核 F2）★ 形态由 base version（该 commit 前最近的 tag）
    # 决定，权威来源 go.dev/ref/mod#pseudo-versions。原注释写"两种"并据此收口——那不是笔误
    # 而是**枚举本身错了**（[[swarm-enumeration-needs-authoritative-source]]）。
    # `_PSEUDO` 的窄口径只认 ③，其老消费者 `_is_stable` 靠"主体含 `-` 即非稳定"把 ①② 兜住了，
    # 所以那边不是 bug；本档判漏的后果相反 = 误杀（血规 10 第三条）。
    "v0.0.0-20230101120000-abcdef123456",         # ① 无 base tag（最常见）
    "v1.2.3-beta.0.20230101120000-abcdef123456",  # ② base 是预发布（golang.org/x/*、k8s.io/* 大量）
    "v1.2.4-0.20230101120000-abcdef123456",       # ③ base 是正式版（patch 递增 + `-0.` 段）
    "v1.2",                                  # 非规范（go.mod 要三段）
])
def test_go_unjudgeable_versions_are_never_probed(monkeypatch, ver):
    """非规范 semver tag **绝不判定**（Maven `${...}` 的对应物）。

    逐条 parametrize + 把探测换成会炸的实现＝同时锁"不判时不查网"。伪版本尤其重要：
    它是真实可用的形态，判它必然 404 → 误杀。
    ★R3 之后 `latest`/分支名/裸 SHA 不再归此档★ 它们写不进 go.mod（语法错误），
    走校正/丢弃分支（见 test_go_ungomoddable_versions_*）。
    """
    def _boom(m, v):
        raise AssertionError(f"不该为 {ver} 探测 proxy（不判形态）")
    monkeypatch.setattr(gr, "proxy_version_exists", _boom)
    kept, _, dropped = gr.resolve_go_deps([f"github.com/x/y@{ver}"])
    assert dropped == []
    assert [(d.module, d.version) for d in kept] == [("github.com/x/y", ver)]


@pytest.mark.parametrize("ver", ["latest", "master", "main", "abcdef1234567890"])
def test_go_ungomoddable_versions_are_corrected_or_dropped(monkeypatch, ver):
    """★R3★ `latest`/分支名/裸 SHA 在 go.mod 里是**语法错误**（`go build` 解析期全灭），
    与伪版本同性质（不可复现）但**不能**原样保留——保留等于把解析错误烤进权威模板。

    治法：先尝试 `proxy_latest_version` 校正到可解析稳定版；校正不到 → 如实丢弃。
    与 npm 不对称是对的：npm 的 `latest` 是合法语法（装最新），go 的不是。
    """
    # 有可用稳定版 → 校正
    monkeypatch.setattr(gr, "proxy_latest_version", lambda m: "v1.9.1")
    kept, _, dropped = gr.resolve_go_deps([f"github.com/x/y@{ver}"])
    assert dropped == []
    assert [(d.module, d.version, d.source, d.verified) for d in kept] == \
        [("github.com/x/y", "v1.9.1", "proxy", "verified")]
    # 无可用稳定版（离线）→ 如实丢弃（fail-honest，血规 2）
    monkeypatch.setattr(gr, "proxy_latest_version", lambda m: None)
    kept, _, dropped = gr.resolve_go_deps([f"github.com/x/y@{ver}"])
    assert kept == []
    assert dropped == [f"github.com/x/y@{ver}"]


# ══════════════════════════════════════════════════════════════════
# ③ F3：npm 侧"包根本不存在"必须与"不可达"机读可辨
# ══════════════════════════════════════════════════════════════════

def test_f3_npm_package_not_found_is_dropped_not_kept(monkeypatch):
    """★F3★ `registry_all_versions` 返 `None` 时"包不存在"与"不可达"原先不可分——
    同一 WARNING 措辞下藏着两种性质相反的事实。

    治法：`registry_package_exists` 先 probe 存在性。npm registry 的 404 是**权威**的
    "包不存在"（与 go proxy 的"proxy 不提供"不同——go 命令拿到 404 会 fallback direct）。
    确证不存在 ⇒ 幻觉包名 ⇒ npm install 必然失败 ⇒ 必须丢弃，不能 fail-open 保留。
    """
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    monkeypatch.setattr(nr, "_http_get", lambda url: None)   # registry_all_versions 返 None
    monkeypatch.setattr(nr, "_http_probe", lambda url: False)  # 404 确证不存在
    kept, dropped = nr.resolve_npm_deps(None, ["ghost-pkg@^1.0.0"])
    assert kept == []
    assert dropped == ["ghost-pkg@^1.0.0"]


def test_f3_npm_package_unreachable_is_fail_open_kept(monkeypatch):
    """反向锁：不可达（不是"不存在"）必须 fail-open 保留，绝不能据不完整证据判幻觉。

    ★断言宽度★ 必须断 `verified`（F-2 的机读账），否则"校正成功却记成 unverified"
    的突变照样绿（harness 实测）。
    ★接线可证伪★ `_http_probe` 返 None 时 `registry_package_exists` 也返 None，
    与"probe 判据整块消失"（`_exists = None`）**行为相同**——整块删掉后 fail-open
    分支照样进，只是少了一次网络调用。判据必须是"probe 被调用过"，否则这是
    血规 10 第四条的形状（`return None` 与"没跑"不可分）。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    monkeypatch.setattr(nr, "_http_get", lambda url: None)
    called = []
    monkeypatch.setattr(nr, "registry_package_exists",
                        lambda name: called.append(name) or None)
    kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert called == ["axios"], "probe 存在性判据没被调用 ⇒ 整块消失后无人发现"
    assert [(k.name, k.spec, k.verified) for k in kept] == \
        [("axios", "^1.6.0", "unverified")]
    assert dropped == []


def test_f3_npm_package_exists_but_versions_malformed_is_fail_open(monkeypatch):
    """第三态：probe 答"包存在"但 `versions` 字段为空/畸形 ⇒ 说明是 registry 文档格式异常，
    按不可达 fail-open（不是"包不存在"）。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    monkeypatch.setattr(nr, "_http_get", lambda url: json.dumps({"name": "axios"}))
    monkeypatch.setattr(nr, "_http_probe", lambda url: True)
    kept, dropped = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert [(k.name, k.spec, k.verified) for k in kept] == \
        [("axios", "^1.6.0", "unverified")]
    assert dropped == []


def test_f3_probe_cache_has_no_negative_stickiness(monkeypatch):
    """★F5 同型回归锁★ npm 侧新增的 `_probe_cache` 也不能把 `None` 永久钉死。"""
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    fn, calls = _flaky_then_ok(99, "")
    monkeypatch.setattr(urllib.request, "urlopen", fn)

    assert nr._http_probe("https://registry.npmjs.org/axios") is None
    n1 = calls["n"]
    assert nr._http_probe("https://registry.npmjs.org/axios") is None
    assert calls["n"] > n1, "None 进 _probe_cache 了（F5 病在 npm 侧复发）"
    assert not nr._probe_cache, "None 不该进 _probe_cache"


def _flaky_then_ok(fail_times: int, body: str):
    """前 `fail_times` 次抛 URLError，之后成功。返回 (fn, 调用计数 dict)。"""
    calls = {"n": 0}

    def fn(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise urllib.error.URLError("模拟网络抖动")
        class _R:
            status = 200
            def read(self): return body.encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return _R()

    return fn, calls


import json  # noqa: E402  (文件内首次使用在上方测试中)
import urllib.request  # noqa: E402


@pytest.mark.parametrize("ver", [
    "v0.0.0-2023010112000-abcdef123456",      # 时间戳 13 位（伪版本恒 14 位）
    "v0.0.0-20230101120000-abcdef12345",      # hash 11 位（恒 12 位）
    "v0.0.0-20230101120000-zzzzzz123456",     # hash 非 16 进制
    "v1.2.3",                                 # 普通 semver
    "v1.2.3-beta.1",                          # 普通预发布（**不是**伪版本）
])
def test_f2_pseudo_lookalikes_are_still_judged(monkeypatch, ver):
    """★F2 的反向锁★ 放宽 `_PSEUDO_ANY` 不许把闸拧成"什么都不判"。

    这五种长得像伪版本但**不是**（时间戳/hash 位数不符、hash 非 16 进制、普通预发布）。
    若它们也落进"不判"分支，Go 侧的幻觉版本治理就整体免检了——那比 F2 原病灶更坏。
    故本条断它们**仍被送去探测**（与上一条的 `_boom` 恰好反向）。
    ★为什么必须有这一档★ 只验"三形态被认出来"的话，把正则改成 `.*` 也全绿
    （[[swarm-mutation-harness-must-check-baseline-green.md]] 同族：单向断言放过过宽治法）。
    """
    seen = []
    monkeypatch.setattr(gr, "proxy_version_exists", lambda m, v: seen.append((m, v)) or True)
    kept, _, dropped = gr.resolve_go_deps([f"github.com/x/y@{ver}"])
    assert seen == [("github.com/x/y", ver)], f"{ver} 被误划进「不判」形态"
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


def test_f5_unreachable_is_not_cached_but_verdicts_are(monkeypatch):
    """★P-C2 复核 F5★ `None`（不可达）**不入探测缓存**，`True`/`False` 仍入。

    原实现无条件 `_probe_cache[_key] = out`，而命中判据是 `if _key in _probe_cache`
    （`None` 也算命中）＋生产侧无任何清理点＋brain 是长驻进程 ⇒ 一次网络抖动就把该
    module@version **永久**钉成"不可达"，此后再不重验，且它长得和"真的一直不可达"
    一模一样（血规 10 第四条：缺席必须机读可辨）。

    ★两个方向都要断★ 只验"`None` 不入缓存"的话，把缓存整个关掉（或每次都 `clear()`）
    也全绿——那把治法退化成"取消缓存"，白丢省 I/O 的收益。故后半段锁住确定性结论**仍然**
    只查一次网（[[swarm-redundant-defense-unfalsifiable]] 的反面：每条断言各自承重）。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    gr._probe_cache.clear()
    gr._http_cache.clear()
    url = "https://x/flaky.info"

    # ① 抖动一次 → None，且**不留痕**
    calls = []

    def _flaky(req, timeout=None):
        calls.append(1)
        raise gr.urllib.error.URLError("connection reset")

    monkeypatch.setattr(gr.urllib.request, "urlopen", _flaky)
    assert gr._http_probe(url) is None
    # 键名与生产实现同源（`_http_probe` 内 `_key = f"probe::{url}"`）。单句直断，不用
    # `A or B` ——`or` 是逃生门：空 dict 也能满足"没有 None 值"那半句 ⇒ 零区分力。
    assert f"probe::{url}" not in gr._probe_cache, \
        f"不可达被写进缓存 → 该 module@version 被永久钉死: {gr._probe_cache}"

    # ② 网络恢复 → 必须**重新探测**并拿到真结论（原实现在这里返回缓存的 None）
    monkeypatch.setattr(gr.urllib.request, "urlopen", _fake_urlopen(status=200))
    assert gr._http_probe(url) is True, "抖动被永久缓存 → 恢复后仍判不可达"

    # ③ 反向锁：确定性结论**仍要**缓存——再问一次不得再发请求
    before = len(calls)
    monkeypatch.setattr(gr.urllib.request, "urlopen", _flaky)
    assert gr._http_probe(url) is True, "确定性结论没入缓存 ⇒ 治法退化成「把缓存关了」"
    assert len(calls) == before, "命中缓存却仍发了请求"

    # ④ 404（确证不存在）同样要缓存
    gr._probe_cache.clear()
    hits = []

    def _404(req, timeout=None):
        hits.append(1)
        raise gr.urllib.error.HTTPError(url, 404, "nf", {}, None)

    monkeypatch.setattr(gr.urllib.request, "urlopen", _404)
    assert gr._http_probe(url) is False
    assert gr._http_probe(url) is False
    assert len(hits) == 1, "False 没入缓存"
