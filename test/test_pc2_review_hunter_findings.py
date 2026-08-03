"""P-C2 复核（silent-failure-hunter）F-1/F-2/F-3/F-4/F-5 的回归锁。

commit b194e79（R1/F1/F2/F5）落地后独立复核逮到五条，本文件逐条锁死。每条测试的判据都是
**把被测机制整块删掉/改回旧写法会不会红**（方法论硬检查②），而不是"实现细节长什么样"。

- F-1：`_http_cache` 永久缓存 `None`（go/npm/maven 三处同型）⇒ 一次抖动把坐标永久钉死。
        F5 只治了 `_probe_cache`，同文件的兄弟函数 `_http_get` 被漏掉（纪律 5 的形状）。
- F-2：三种结局（确证/不可达 fail-open/刻意不判）全塌成 `source="explicit"` ⇒ 闸整轮
        静默失效时交付物与闸正常时逐字相同。
- F-3：撤掉负缓存后无退避 ⇒ per-module 调用下代价被镜像数 × 模块数放大（与 F-1 互为张力，
        所以治法是 TTL 而非"不缓存"，两个方向都要锁）。
- F-4：R1 收了 `^`/`~` 却漏了 `>` 臂（同函数第三种口径）。
- F-5：`_PSEUDO_ANY` 为覆盖官方三形态而放宽过头 ⇒ 成了跳过存在性核验的通道。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from swarm.brain import dep_http_cache as dhc
from swarm.brain import go_registry as gr
from swarm.brain import npm_registry as nr
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


@pytest.fixture(autouse=True)
def _clean_caches():
    for mod in (gr, nr):
        mod._http_cache.clear()
        mod._http_neg_until.clear()
    gr._probe_cache.clear()
    yield
    for mod in (gr, nr):
        mod._http_cache.clear()
        mod._http_neg_until.clear()
    gr._probe_cache.clear()


class _Resp:
    status = 200

    def __init__(self, body: str) -> None:
        self._b = body.encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _flaky_then_ok(fail_times: int, body: str):
    """前 `fail_times` 次抛 URLError，之后成功。返回 (fn, 调用计数 dict)。"""
    calls = {"n": 0}

    def fn(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise urllib.error.URLError("模拟网络抖动")
        return _Resp(body)

    return fn, calls


# ═══════════════════ F-1：一次抖动不得把坐标永久钉死 ═══════════════════

def test_f1_transient_failure_does_not_stick_forever(monkeypatch):
    """★F-1（CRITICAL）★ 抖动 → TTL 过期 → 网络已恢复 ⇒ 必须重新问网并拿到真答案。

    旧写法（`_http_cache[url] = text` 无条件 + `if url in _http_cache` 命中）下：
    `None` 永久命中 ⇒ 第三次询问一次网都不发 ⇒ 该 module 在整个 brain 进程生命周期里
    被判"查不到" ⇒ `resolve_go_deps` 照旧 dropped，而它和"proxy 真没这个包"共用同一条
    WARNING 措辞、同一条 dropped 路径，**没有任何键能区分**。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    fn, calls = _flaky_then_ok(2, '{"Version":"v1.9.1"}')   # 两个镜像各抖一次
    monkeypatch.setattr(urllib.request, "urlopen", fn)

    assert gr.proxy_latest_version("github.com/gin-gonic/gin") is None, "抖动期应为 None"
    n_after_outage = calls["n"]
    assert n_after_outage == 2, f"两个镜像都该被问到（F1 冗余），实际 {n_after_outage}"

    # 模拟 TTL 自然到期（只把到期时刻拨到过去，不碰缓存值本身）
    for k in list(gr._http_neg_until):
        gr._http_neg_until[k] = 0.0

    assert gr.proxy_latest_version("github.com/gin-gonic/gin") == "v1.9.1", \
        "TTL 过期后必须重新联网核验（旧写法在这里永久返回 None＝误杀真依赖）"
    assert calls["n"] > n_after_outage, "根本没重新问网 ⇒ 负缓存仍是永久的"


def test_f1_success_clears_negative_record():
    """成功写入必须清掉该 key 的负记录（否则陈旧 TTL 无界累积）。

    ★为什么在**单元层**直接调 `text_cache_store` 而不走 `_http_get`★
    走 `_http_get` 的话这条断言**零区分力**（突变 harness 实测：把 store 侧的 pop 拧成
    `pass`，经 `_http_get` 的版本照旧全绿）。原因是同一件事被清了两遍：
    `text_cache_lookup` 处理"None 且已过期"时就把 cache 和 neg_until **两个** dict 一起
    pop 了，等 `text_cache_store` 跑到时 neg_until 早已干净 ⇒ store 侧的 pop 无从被观测。
    冗余防御 = 任一处单独突变都仍绿 = 两处都不可证伪。

    ★那为什么不干脆删掉 store 侧的 pop★ 它不是死代码：仓里有 26 处既有测试只
    `_http_cache.clear()` 而**不**清 `_http_neg_until`（它们早于这个 dict）。清完之后
    cache 空、neg_until 留键 ⇒ 下次 lookup 因"key 不在 cache"直接 miss（走不到过期清理那
    一支）⇒ 成功后 store 侧这个 pop 是唯一清掉陈旧记录的地方。故保留机制、把断言下沉到
    能隔离它的那一层。
    """
    cache: dict = {}
    neg: dict = {}
    key = "https://x/y"

    dhc.text_cache_store(cache, neg, key, None)
    assert neg.get(key, 0.0) > 0, "失败写入没记 TTL"
    # 模拟"cache 被清而 neg 未清"（26 处既有测试的真实写法）后又成功取到文本
    cache.pop(key)
    dhc.text_cache_store(cache, neg, key, "OK")
    assert cache[key] == "OK"
    assert key not in neg, "成功写入后仍留负记录 ⇒ 陈旧 TTL 无界累积"


def test_f1_unknown_none_without_ttl_is_revalidated(monkeypatch):
    """来历不明的 `None`（无 TTL 记录）按"已过期"处理 ⇒ 重新核验，且就地清理不留垢。

    方向刻意选"宁可多验一次"：凭一个来历不明的 None 误杀真依赖，代价高于多一次 HTTP。
    """
    key = "https://x/stale"
    gr._http_cache[key] = None                             # 无对应 _http_neg_until 记录
    hit, val = dhc.text_cache_lookup(gr._http_cache, gr._http_neg_until, key)
    assert hit is False and val is None, "无 TTL 记录的 None 被当成命中 ⇒ F-1 会复现"
    assert key not in gr._http_cache, "过期条目未就地清理（两个 dict 会无界增长）"


def test_f1_all_three_registries_wired(monkeypatch):
    """★接线覆盖：三个 registry 一个不落★（纪律 5：修一类先全仓捞 sibling）。

    F5 当初只治 `_probe_cache` 就是漏了兄弟函数。这里逐模块证"负缓存有 TTL 语义"，
    而不是只测 go 一个然后假定另两个也改了。
    """
    from swarm.brain import maven_registry as mr

    for mod, env in ((gr, "SWARM_GO_LOOKUP"), (nr, "SWARM_NPM_LOOKUP"),
                     (mr, "SWARM_MAVEN_LOOKUP")):
        mod._http_cache.clear()
        mod._http_neg_until.clear()
        monkeypatch.setenv(env, "1")
        fn, calls = _flaky_then_ok(1, "OK")
        monkeypatch.setattr(urllib.request, "urlopen", fn)
        url = f"https://example.invalid/{mod.__name__}"

        assert mod._http_get(url) is None, f"{mod.__name__} 首次应失败"
        assert url in mod._http_neg_until, f"{mod.__name__} 没记 TTL ⇒ 仍是永久负缓存"
        assert mod._http_get(url) is None, f"{mod.__name__} TTL 内应直接命中不重复烧网"
        assert calls["n"] == 1, f"{mod.__name__} TTL 内又打网了（F-3 代价放大）"
        mod._http_neg_until[url] = 0.0
        assert mod._http_get(url) == "OK", f"{mod.__name__} TTL 过期后没重试（F-1 未治）"


# ═══════════════════ F-3：TTL 内不得重复烧网络（与 F-1 反方向） ═══════════════════

def test_f3_ttl_window_suppresses_repeat_network_cost(monkeypatch):
    """★F-3（MEDIUM）★ 与 F-1 互为张力，两个方向都要锁。

    `resolve_*_deps` 是 **per-module** 调用（`_inject_go_scaffolds` 的 `for entry in mods_all`），
    `seen` 去重只在单次调用内生效；叠上 F1 把"首个镜像不可达即早返"改成"两个都问"，
    代价 = 超时 × 镜像数 × 模块数 × 依赖数。若照 F5 的办法完全不缓存 `None`，
    这里的 `calls["n"]` 会随询问次数线性增长（分钟级纯等待，且不报错只是"规划变慢"）。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    fn, calls = _flaky_then_ok(99, "")          # 恒不可达
    monkeypatch.setattr(urllib.request, "urlopen", fn)

    for _ in range(5):                          # 模拟 5 个模块问同一个坐标
        assert gr.proxy_latest_version("github.com/x/y") is None
    assert calls["n"] == 2, (
        f"TTL 内应只在首次问两个镜像，实际 {calls['n']} 次 ⇒ 代价被模块数放大（F-3）")


def test_f3_probe_cache_still_has_no_negative_stickiness(monkeypatch):
    """F5 的 `_probe_cache` 语义未被本批改动波及：`None` 仍不入缓存（回归锁）。

    两个缓存的策略**刻意不同**：文本缓存用 TTL（F-3 的代价来自它），探测缓存不缓存 None
    （单次 plan 内探测次数远少，且 F1 已要求两镜像都问）。改共享代码时这条防串味。
    """
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    fn, calls = _flaky_then_ok(99, "")
    monkeypatch.setattr(urllib.request, "urlopen", fn)

    assert gr._http_probe("https://proxy.golang.org/x/@v/v1.0.0.info") is None
    n1 = calls["n"]
    assert gr._http_probe("https://proxy.golang.org/x/@v/v1.0.0.info") is None
    assert calls["n"] > n1, "_probe_cache 又开始粘滞 None 了（F5 被回退）"
    assert not gr._probe_cache, "None 不该进 _probe_cache"


# ═══════════════════ F-4：`>` 臂的缺位语义 ═══════════════════

@pytest.mark.parametrize(("spec", "versions", "expected"), [
    # npm 官方明文："The comparator `>1` is equivalent to `>=2.0.0`"，并列 1.0.1/1.1.0 不匹配
    (">1", {"1.0.1"}, False),
    (">1", {"1.1.0"}, False),
    (">1", {"1.9.9"}, False),
    (">1", {"2.0.0"}, True),
    (">1", {"3.1.0"}, True),
    # `>1.2` → `>=1.3.0`：官方无明文，按 desugaring 表（`1.2 := >=1.2.0 <1.3.0-0`）同规则推出
    (">1.2", {"1.2.5"}, False),
    (">1.2", {"1.3.0"}, True),
    (">1.2", {"2.0.0"}, True),
    # 三段全写：`>` 就是逐元素比较，不受 prec 影响（原行为，防我把它一起改坏）
    (">2.0.0", {"2.0.0"}, False),
    (">2.0.0", {"2.0.1"}, True),
    (">1.9.0", {"1.9.1"}, True),
    # `>=` 臂：补零的 floor 正是下界本身，缺位无需特殊处理（对照组，证明我没误改它）
    (">=1", {"1.0.0"}, True),
    (">=1", {"0.9.9"}, False),
    (">=1.2", {"1.2.0"}, True),
])
def test_f4_gt_arm_treats_missing_segments_as_xrange(spec, versions, expected):
    """★F-4（MEDIUM）★ R1 收了 `^`/`~` 却漏了 `>`——同函数内第三种口径。

    方向是**判宽（假过）**而非误杀：`express@>18` 而仓库最高 18.3.1 这类真幻觉会被放行
    进 plan，执行期 `npm install` 才炸。旧集里 `>` 三格恰好都是三段全写，绕开了缺位。
    """
    assert nr._range_is_satisfiable(spec, frozenset(versions)) is expected


def test_f4_did_not_regress_caret_tilde_arms():
    """R1 已治的 `^`/`~` 六格不受 F-4 改动影响（改共享函数必复跑兄弟断言）。"""
    cases = [("~18", {"18.3.1"}, True), ("^0", {"0.5.2"}, True), ("^0.0", {"0.0.9"}, True),
             ("^0.0.3", {"0.0.4"}, False), ("~1.2", {"1.3.0"}, False), ("^1", {"1.9.9"}, True)]
    for spec, vers, exp in cases:
        assert nr._range_is_satisfiable(spec, frozenset(vers)) is exp, spec


# ═══════════════════ F-5：伪版本模式不得宽于官方三形态 ═══════════════════

@pytest.mark.parametrize(("ver", "is_pseudo"), [
    # 官方三形态（go.dev/ref/mod#pseudo-versions）
    ("v0.0.0-20240101000000-abcdef123456", True),               # ① 无 base tag
    ("v1.2.3-beta.0.20240101000000-abcdef123456", True),        # ② base 是预发布
    ("v1.2.3-rc.1.0.20240101000000-abcdef123456", True),        # ② 多段 <pre>
    ("v1.2.4-0.20240101000000-abcdef123456", True),             # ③ base 是正式版
    ("v0.0.0-20240101000000-abcdef123456+incompatible", True),  # ① + incompatible
    # ★放宽过头会放行的形态★ `.0.` 是规范的一部分，不是任意段
    ("v1.2.3-beta.7.20240101000000-abcdef123456", False),
    ("v1.2.3-totally.made.up.20240101000000-abcdef123456", False),
    ("v1.2.3-ABCDEF.20240101000000-ABCDEF123456", False),       # 大写 hash：go 自己就拒
    # 位数反向锁
    ("v1.2.3-beta.0.2024010100000-abcdef123456", False),        # ts 13 位
    ("v1.2.3-beta.0.20240101000000-abcdef12345", False),        # hash 11 位
    ("v1.2.3-beta.0.20240101000000-abcdefg23456", False),       # hash 非 hex
    # 普通版本不得被当伪版本
    ("v1.2.3-rc.1", False),
    ("v1.2.3", False),
])
def test_f5_pseudo_pattern_matches_official_forms_only(ver, is_pseudo):
    """★F-5（LOW）★ 为覆盖官方三形态而把前缀放宽成"任意串+点"⇒ 成了**跳闸通道**：
    命中即走"不判"分支＝跳过存在性核验原样保留，L1 期 `go mod download` 才失败，
    归因会落到 worker 身上。两个方向的原有测试各自都绿（正向证三形态被认、反向证
    lookalike 被拒），中间这条"合法时间戳+合法 hash+前缀乱写"的带子谁都没测。
    """
    assert bool(gr._PSEUDO_ANY.search(ver)) is is_pseudo


def test_f5_judgeable_and_pseudo_are_complementary_on_official_forms():
    """伪版本必须同时能过 `_JUDGEABLE_VERSION`——否则 `or` 短路后走哪条分支说不清。

    这条锁的是**两个模式的关系**而非各自形状：生产判据是
    `not _JUDGEABLE_VERSION.match(x) or _PSEUDO_ANY.search(x)`，若官方伪版本连
    JUDGEABLE 都不过，第一个子句就已成立，F2/F-5 这一整段等于死代码。
    """
    official = "v1.2.3-beta.0.20240101000000-abcdef123456"
    assert gr._JUDGEABLE_VERSION.match(official), "官方形态 ② 过不了 JUDGEABLE ⇒ F2 段是死代码"
    assert gr._PSEUDO_ANY.search(official)


# ═══════════════════ F-2：闸"没能证实"必须机读可辨且有人消费 ═══════════════════

def _st(sid, create):
    return SubTask(id=sid, description=f"task {sid}", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=[], create_files=create))


def _npm_plan():
    plan = TaskPlan(subtasks=[_st("st-1", ["packages/core/src/index.ts"]),
                              _st("st-2", ["packages/web/src/app.ts"])],
                    parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "core", "artifacts": ["axios@^1.6.0"]},
        {"module": "web", "artifacts": ["lodash@~4", "core"]},
    ]}
    return plan


@pytest.fixture
def _npm_root(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "root", "private": True, "workspaces": ["packages/*"]}),
        encoding="utf-8")
    monkeypatch.setenv("SWARM_NPM_LOOKUP", "1")
    return tmp_path


def test_f2_three_outcomes_are_machine_distinguishable():
    """★F-2（HIGH）★ 三种结局原先全塌成 `source="explicit"`。

    ★刻意用生产真实路径取值，不手工构造 `verified=`★（假绿典型＝构造生产代码从不产生的取值）：
    `SWARM_*_LOOKUP=0` ⇒ 恒不可达 ⇒ 走 fail-open 分支；伪版本/协议 ⇒ 走"不判"分支。
    """
    import os

    os.environ["SWARM_GO_LOOKUP"] = "0"
    os.environ["SWARM_NPM_LOOKUP"] = "0"
    try:
        kept, _, _ = gr.resolve_go_deps(["github.com/gin-gonic/gin@v1.9.1"])
        assert [(k.source, k.verified) for k in kept] == [("explicit", "unverified")]

        kept2, _, _ = gr.resolve_go_deps(
            ["github.com/x/y@v0.0.0-20240101000000-abcdef123456"])
        assert [k.verified for k in kept2] == ["unjudgeable"]

        kn, _ = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
        assert [(k.source, k.verified) for k in kn] == [("explicit", "unverified")]

        kn2, _ = nr.resolve_npm_deps(None, ["foo@npm:bar@^1.0.0"])
        assert [k.verified for k in kn2] == ["unjudgeable"]
    finally:
        os.environ.pop("SWARM_GO_LOOKUP", None)
        os.environ.pop("SWARM_NPM_LOOKUP", None)


def test_f2_verified_when_registry_confirms(_npm_root, monkeypatch):
    """反向锁：registry 可达且版本集能满足 ⇒ `verified`（防"永远报未证实"的假阳）。"""
    monkeypatch.setattr(nr, "_http_get", lambda url: json.dumps(
        {"dist-tags": {"latest": "1.6.8"}, "versions": {"1.6.0": {}, "1.6.8": {}}})
        if "axios" in url else None)
    kept, _ = nr.resolve_npm_deps(None, ["axios@^1.6.0"])
    assert [(k.source, k.verified) for k in kept] == [("explicit", "verified")]


def test_f2_unverified_reaches_the_machine_readable_ledger(_npm_root, monkeypatch):
    """★接线证明：账要真的到得了 finish_plan_deterministic 的 out★

    只断 `verified` 字段存在＝证"数据里有这个区分"，证不了"有人收得到"。本条走
    **公开入口** `inject_build_scaffold_subtasks(..., unverified_out=...)`，锁住整条链。
    """
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks

    monkeypatch.setattr(nr, "_http_get", lambda url: None)      # registry 恒不可达
    out: dict = {}
    injected = inject_build_scaffold_subtasks(_npm_plan(), str(_npm_root), None,
                                              unverified_out=out)
    assert injected, "夹具没走到 npm driver（零注入 ⇒ 这条测试什么也没证明）"
    flat = [s for v in out.values() for s in v]
    assert any("unverified" in s for s in flat), f"未证实坐标没进账: {out}"
    # ★断**完整坐标**而不只是包名★ 突变实验实测：把版本字段拧成只认 go 的 `version`
    # （npm 的 `spec` 读不到 ⇒ 记成 `axios@?`）时，只断 `"axios" in s` 照旧全绿——
    # 名字还在，而"记了一笔但认不出是哪个版本"等于这笔账没用。
    assert "axios@^1.6.0(unverified)" in flat, f"账里的坐标不完整（名字/版本/档位缺一）: {flat}"


def test_f2_ledger_empty_when_all_verified(_npm_root, monkeypatch):
    """全部证实时账必须为空——always-report 会让这个键失去信息量（等于没造）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks

    def fake(url):
        if "axios" in url:
            return json.dumps({"dist-tags": {"latest": "1.6.8"},
                               "versions": {"1.6.0": {}, "1.6.8": {}}})
        if "lodash" in url:
            return json.dumps({"dist-tags": {"latest": "4.17.21"},
                               "versions": {"4.17.21": {}}})
        return None

    monkeypatch.setattr(nr, "_http_get", fake)
    out: dict = {}
    assert inject_build_scaffold_subtasks(_npm_plan(), str(_npm_root), None,
                                          unverified_out=out)
    assert out == {}, f"可满足却报未证实＝误报: {out}"


def _go_plan():
    plan = TaskPlan(subtasks=[_st("st-1", ["svc/auth/main.go"]),
                              _st("st-2", ["svc/gateway/main.go"])],
                    parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "auth", "artifacts": ["github.com/golang-jwt/jwt/v5@v5.2.0"]},
        {"module": "gateway", "artifacts": ["github.com/gin-gonic/gin@v1.9.1", "auth"]},
    ]}
    return plan


@pytest.fixture
def _go_root(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n", encoding="utf-8")
    monkeypatch.setenv("SWARM_GO_LOOKUP", "1")
    monkeypatch.setenv("GOPATH", str(tmp_path / "_empty_gopath"))
    return tmp_path


def test_f2_ledger_is_stack_neutral_go_side(_go_root, monkeypatch):
    """★栈中立：go 侧的坐标也要记得出名字★（纪律 1，多栈中立）

    `_record_unverified_deps` 用 `getattr(k, "module", None) or getattr(k, "name", "?")`
    兼容两栈字段名（go 是 module/version，npm 是 name/spec）。**只测 npm 的话这条 go 分支
    从未被执行**——突变 harness 实测：把它拧成只认 `name`，原先那条只跑 npm 的账测试照旧
    全绿，而 go 侧会把每个坐标记成 `?@?`（账在、但内容无法定位到具体依赖 = 等于没记）。
    """
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks

    # ★两个都要 stub★ 显式版本的存在性核验走 `_http_probe`（不是 `_http_get`）。只 stub 后者
    # 时本条测试会**依赖这台机器有没有网**：有网 ⇒ gin v1.9.1 真存在 ⇒ 判 verified ⇒ 账为空
    # ⇒ 测试红得莫名其妙；离线 ⇒ 偶然绿。项目纪律要求测试绝不依赖网络（"网络好就绿、
    # 离线就红"的假绿）。这里显式构造"proxy 不可达"这一确定场景。
    monkeypatch.setattr(gr, "_http_get", lambda url: None)
    monkeypatch.setattr(gr, "_http_probe", lambda url: None)
    out: dict = {}
    injected = inject_build_scaffold_subtasks(_go_plan(), str(_go_root), None,
                                              unverified_out=out)
    assert injected, "夹具没走到 go driver（零注入 ⇒ 这条测试什么也没证明）"
    flat = [s for v in out.values() for s in v]
    assert flat, f"go 侧未证实坐标没进账: {out}"
    assert not any(s.startswith("?@") for s in flat), \
        f"go 坐标被记成 ?@?（记账函数只认 npm 字段名）: {flat}"
    # 同 npm 侧：断完整坐标，否则"版本字段读不到"这一维不在被测命题里
    assert "github.com/gin-gonic/gin@v1.9.1(unverified)" in flat, \
        f"go 账里的坐标不完整（module/version/档位缺一）: {flat}"
    assert all("unverified" in s or "unjudgeable" in s for s in flat), flat


def test_a1_maven_dep_carries_verified_three_tiers(tmp_path, monkeypatch):
    """★复核 A-1★ maven 也要有三档。**RuoYi 基线就是 Maven＝本项目主栈**，原先它完全不入账：
    Central 不可达时显式版本全部 fail-open 保留，而账是 `{}` ⇒ 与"全部证实"逐字相同，
    F-2 要治的病在主栈原封不动。

    `SWARM_MAVEN_LOOKUP=0` ⇒ 仓库恒不可达（生产真实路径，非手工构造取值）。
    """
    from swarm.brain import maven_registry as mr

    monkeypatch.setenv("SWARM_MAVEN_LOOKUP", "0")
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion><groupId>com.x</groupId>"
        "<artifactId>app</artifactId><version>1</version></project>", encoding="utf-8")

    kept, _ = mr.resolve_artifacts(str(tmp_path), ["com.a:b:1.2.3"])
    assert [(k.source, k.verified) for k in kept] == [("explicit", "unverified")], \
        "仓库不可达却记成已证实（A-1 主栈缺口）"

    kept2, _ = mr.resolve_artifacts(str(tmp_path), ["com.a:b:${x.version}"])
    assert [k.verified for k in kept2] == ["unjudgeable"], \
        "${...} 属性引用应为不判档（判它只会误杀）"


def test_a1_maven_record_fn_is_stack_neutral():
    """★三栈中立第三栈（纯函数层）★ maven 的 `ResolvedDep` 字段是 group/artifact
    （**没有** module/name）⇒ 记账函数若只认 go/npm 字段名，maven 全被记成 `?@...`：
    账在、却认不出是哪个依赖。这正是 go 侧被突变实验逮到的同一形状。
    """
    from swarm.brain.contract_utils import _record_unverified_deps
    from swarm.brain.maven_registry import ResolvedDep

    out: dict = {}
    _record_unverified_deps(out, "app", [
        ResolvedDep(group="com.a", artifact="b", version="1.2.3",
                    source="explicit", verified="unverified"),
        ResolvedDep(group="com.c", artifact="d", version=None,
                    source="registry", verified="unverified"),
    ])
    flat = [s for v in out.values() for s in v]
    assert not any(s.startswith("?@") for s in flat), f"maven 坐标记成 ?@（漏第三栈）: {flat}"
    assert "com.a:b@1.2.3(unverified)" in flat, f"坐标不完整: {flat}"
    # 无版本那条要能看出"没有版本"，而不是记成 `?`（与"字段读不到"混淆）
    assert "com.c:d@<无版本>(unverified)" in flat, f"无版本标记不可辨: {flat}"


def test_a1_maven_reaches_ledger_through_public_entry(tmp_path, monkeypatch):
    """★接线层：必须走**公开入口**，否则测的是函数不是接线★

    ★这条测试的前身是假绿★：它原先直接调 `_record_unverified_deps`，于是把 maven driver 里
    那一行调用整个删掉，它照旧全绿（突变 harness 实测"仍绿"）。**同一形状本会话第三次**
    （前两次：账测试只跑 npm；go 夹具只 stub `_http_get`）。
    夹具形状抄 test_r39_build_scaffold_inject.py::_plan_two_modules（已跑通的 maven 真形状）。
    """
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks

    monkeypatch.setenv("SWARM_MAVEN_LOOKUP", "0")   # 仓库恒不可达 ⇒ 显式版本走 fail-open
    plan = TaskPlan(subtasks=[
        _st("st-1", ["mod-a/src/main/java/A.java"]),
        _st("st-2", ["mod-b/src/main/java/B.java"]),
    ], parallel_groups=[["st-1"], ["st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-a", "artifacts": ["com.a:b:1.2.3"]},
        {"module": "mod-b", "artifacts": ["com.c:d:4.5.6"]},
    ]}

    out: dict = {}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path), None, unverified_out=out)
    assert injected, "夹具没走到 maven 注入路径（零注入 ⇒ 这条测试什么也没证明）"
    flat = [s for v in out.values() for s in v]
    assert flat, f"maven 未证实坐标没经 driver 进账（接线断了）: {out}"
    assert "com.a:b@1.2.3(unverified)" in flat, f"账里坐标不完整: {flat}"


def test_a7_missing_verified_attr_defaults_pessimistic():
    """★复核 A-7 / 纪律 3★ 缺 `verified` 属性的对象必须**进账**而非静默算已证实。

    方向判据：将来第四栈忘加这个字段时，后果应该是"账里多一条（可见）"，
    而不是"这一栈静默从账里消失"。
    """
    from swarm.brain.contract_utils import _record_unverified_deps

    class _NoVerifiedField:
        module = "example.com/x"
        version = "v1.0.0"

    out: dict = {}
    _record_unverified_deps(out, "m", [_NoVerifiedField()])
    assert out == {"m": ["example.com/x@v1.0.0(unverified)"]}, \
        f"缺字段被乐观当成已证实 ⇒ 新栈会静默漏账: {out}"


def test_f2_out_param_is_optional_and_non_invasive(_npm_root, monkeypatch):
    """不传 `unverified_out` 时行为与传了完全一致（既有约 60 个调用点零影响）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks

    monkeypatch.setattr(nr, "_http_get", lambda url: None)
    with_out: dict = {}
    a = inject_build_scaffold_subtasks(_npm_plan(), str(_npm_root), None,
                                       unverified_out=with_out)
    b = inject_build_scaffold_subtasks(_npm_plan(), str(_npm_root), None)
    assert [e["module"] for e in a] == [e["module"] for e in b]
    assert with_out, "传了 out 却没收集到（对照组本身失效，这条测试没有区分力）"


def test_f2_state_key_is_declared_in_brainstate():
    """★LangGraph 未在 schema 声明的键会被静默丢弃★ 声明缺失＝这条账永远到不了 checkpoint。

    同时锁 reducer 档位：必须是 last-write-wins（"round"），绝不能进 append-only
    的 degraded_reasons——愈合后陈旧值无人能清，会永久误拦 should_write_success。
    """
    from swarm.brain.state import BrainState

    assert "dep_versions_unverified" in BrainState.__annotations__, \
        "BrainState 没声明该键 ⇒ plan 节点 return 它也会被 LangGraph 静默丢弃"
    from swarm.brain import state as _state

    reducers = next((v for k, v in vars(_state).items()
                     if isinstance(v, dict) and "dep_ban_reconciled" in v), None)
    assert reducers is not None, "找不到 reducer 表（本条断言的前提没了，别静默放过）"
    assert reducers.get("dep_versions_unverified") == "round", \
        "reducer 档位不是 last-write-wins ⇒ 愈合后陈旧值会粘滞"


def test_a3_ledger_has_a_real_consumer_in_progress_api(monkeypatch):
    """★复核 A-3（阻断项）★ 血规 10 第四条：新账**没有消费者＝没造**。

    此前 `dep_versions_unverified` 生产侧零读点（只有 state 声明 / plan_finisher 写入 /
    plan 节点转发）。而我当初拿 `dep_ban_reconciled` 当"always-emit 观测键"的口径先例，
    复核实测**它自己也零读点** ⇒ 拿没有消费者的键论证没有消费者的键＝循环背书。

    消费面选 `get_task_progress`：它是任务结构化进度的唯一权威出口（纪律 #106 禁止解析
    swarm.log），而"闸整轮静默失效"恰恰是日志有 WARNING、机读面全空。
    刻意只报不拦（非门）——不可达是环境常态，做成闸会被绕开。
    """
    import asyncio

    from swarm.brain import runner

    payload = {"web": ["axios@^1.6.0(unverified)"]}
    fake_state = {
        "dispatch_remaining": [], "subtask_results": {}, "failed_subtask_ids": [],
        "abandoned_subtask_ids": [], "give_up_isolated_ids": [],
        "plan": {"subtasks": [{"id": "s1"}]},
        "dep_versions_unverified": payload,
        "dep_ban_reconciled": {"s1": {"old": "x"}},
        "contract_symbol_paths_unhealed": ["Foo"],
    }

    # 桩形状抄 test_b9_frontend_progress.py::_patch_runner（那是已跑通的真形状，别自己编）
    import swarm.tracing as tracing

    class _Snap:
        values = fake_state

    class _Graph:
        async def aget_state(self, config):
            return _Snap()

    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _Graph())
    monkeypatch.setattr(runner.store, "get_task", lambda tid: {})
    monkeypatch.setattr(tracing, "brain_graph_config", lambda **kw: {})

    p = asyncio.run(runner.get_task_progress("t1"))
    assert p is not None, "夹具失效（端点返回 None）⇒ 这条测试什么也没证明"
    assert p.get("dep_versions_unverified") == payload, \
        "账没被进度端点消费 ⇒ 机读面依旧全空（A-3 未治）"
    # 先例也必须自己满足纪律，否则引用它等于没论证
    assert "dep_ban_reconciled" in p and "contract_symbol_paths_unhealed" in p, \
        "循环背书未消除：先例键仍零消费"


def test_f2_plan_finisher_emits_the_key_always(_npm_root, monkeypatch):
    """always-emit：无命中也要发 `{}`（否则键缺席与"本轮全证实"不可分，且会粘滞上一轮）。"""
    from swarm.brain.plan_finisher import finish_plan_deterministic

    def fake(url):
        if "axios" in url:
            return json.dumps({"dist-tags": {"latest": "1.6.8"},
                               "versions": {"1.6.0": {}, "1.6.8": {}}})
        if "lodash" in url:
            return json.dumps({"dist-tags": {"latest": "4.17.21"},
                               "versions": {"4.17.21": {}}})
        return None

    monkeypatch.setattr(nr, "_http_get", fake)
    out = finish_plan_deterministic(_npm_plan(), None, project_path=str(_npm_root))
    assert "dep_versions_unverified" in out, "全证实时该键缺席 ⇒ 与'没跑过'不可分"
    assert out["dep_versions_unverified"] == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
