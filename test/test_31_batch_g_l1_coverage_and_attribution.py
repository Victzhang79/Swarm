"""31 号文批 G 锁：A3 残余 8 条（L1 闸门覆盖面 + 降级归因）。

八条里 6 条是同一族——**降级/截断/探针失败没有机读账**，与"本来就无事发生"同形，
于是那一层可以死很久没人知道（血规 10④「空返回/缺席必须机读可辨」）。
另 2 条是覆盖面：手抄枚举比权威集窄（A3-M1/L3，一处派生化同时关掉两条）。

★本批最该记的两件事★
1. **报告开的治法不足以关掉它自己点名的缺口**（A3-M1）：`STACK_SPEC["npm"].source_exts`
   当时**也缺** `.mts/.cts`，只"从 source_exts 派生"仍是零覆盖 ⇒ 必须先补权威表。
2. **报告指的位置不可达，但它描述的机制可达**（A3-M3）：`run_command` 全函数 0 条 raise，
   `except` 臂近乎死代码；真正可达的是**只读 `cr.stdout` 不看 `cr.success`**。
"""

from __future__ import annotations

import os

import pytest

from swarm.worker import l1_pipeline as lp


# ───────────────── A3-M1 / A3-L3：后缀集派生化 ─────────────────

def test_m1_authoritative_table_has_ts_module_variants():
    """★权威表本身必须含 `.mts/.cts`★——只做"派生"关不掉这两个后缀。

    这条锁的位置很关键：它钉的是 `STACK_SPEC`，不是 l1_pipeline。若有人认为
    "从 source_exts 派生了就行"而把这两个后缀从权威表删掉，派生侧全绿而覆盖面归零。
    """
    from swarm.stacks.spec import STACK_SPEC

    _npm = STACK_SPEC["npm"].source_exts
    for _e in (".mts", ".cts"):
        assert _e in _npm, f"{_e} 不在 npm 权威后缀集里 ⇒ tsc 触发集派生出来也不含它"


def test_m1_ts_declaration_variants_excluded():
    """★同批必须补的排除面★ `.d.mts`/`.d.cts` 是纯类型声明（无编译产物），不得算源码。

    ★我差点在注释里写"已由 .d.ts 那条排掉"——那是假话★：`.d.mts` 不以 `.d.ts` 结尾。
    加 source_exts 不同步加排除 = 把纯声明文件也算进参与编译源码。
    """
    from swarm.stacks.spec import is_compilable_source

    for _f in ("a.d.ts", "a.d.mts", "a.d.cts"):
        assert is_compilable_source(_f, "npm") is False, f"{_f} 被算成参与编译源码"
    for _f in ("a.mts", "a.cts", "a.ts"):
        assert is_compilable_source(_f, "npm") is True, f"{_f} 应算参与编译源码"


def test_m1_compile_trigger_set_is_derived_not_handwritten():
    """compile 的 js_ts 触发集必须等于权威派生集（原是本文件里第三份手抄且最窄）。"""
    from swarm.stacks.spec import STACK_SPEC

    _expect = {e for s in STACK_SPEC.values() if s.lang == "node" for e in s.source_exts}
    assert set(lp._ext_for_lang("node")) == _expect


@pytest.mark.parametrize("ext", [".mjs", ".cjs", ".mts", ".cts", ".vue", ".jsx"])
def test_m1_all_node_exts_reach_a_gate(ext):
    """★覆盖面锁★ 每个 node 后缀都必须落进某道闸的触发面（tsc 或 node --check）。

    判据：该后缀要么在 `_ext_for_lang("node")`（⇒ 进 tsc/vue-tsc 臂），
    要么在 `_JS_SYNTAX_EXTS`（⇒ 进 node --check 臂）。两者都不在＝该后缀零闸。
    """
    assert ext in lp._ext_for_lang("node") or ext in lp._JS_SYNTAX_EXTS, \
        f"{ext} 不在任何闸的触发面上"


def test_m1_ts_only_set_excludes_pure_js():
    """`_ts_only_syntax_exts` 必须是"只能靠 tsc"的那部分——纯 JS 有 node --check 兜底。

    若把纯 JS 也算进去，`ts_gate_unavailable` 会在 node --check 明明跑了的情况下误报。
    """
    _ts_only = set(lp._ts_only_syntax_exts())
    assert not (_ts_only & set(lp._JS_SYNTAX_EXTS)), \
        f"纯 JS 后缀混进了 ts_only 集: {_ts_only & set(lp._JS_SYNTAX_EXTS)}"
    for _e in (".mts", ".cts", ".ts", ".tsx", ".vue", ".jsx"):
        assert _e in _ts_only, f"{_e} 只能靠 tsc，必须在 ts_only 集里"


def test_l3_lint_groups_vue_into_js_ts(tmp_path, monkeypatch):
    """A3-L3：lint 分组必须把 `.vue` 归进 js_ts（治前 lint 侧后缀表连 `.vue` 都不含）。

    ★必须驱动【调用点】，不能只断 `_ext_for_lang("node")` 含 .vue★
    初版我只断了那个派生函数，于是"把 lint 分组换回手抄表"的突变**全绿**——测的是 helper，
    而 finding 说的是 helper 没被用上。这已是本战役第三次同型（批 C c4 / 批 E e9·e11·e17）。
    现驱动 `_lint_files`，spy 命令执行器，断 `.vue` 真的走到了 js_ts 那条 lint 链上。
    """
    (tmp_path / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.vue").write_text("<template><div/></template>", encoding="utf-8")

    seen: list[str] = []

    def _spy(cmd, project_path, timeout=60):
        seen.append(str(cmd))
        return 0, "", ""            # 假装 lint 通过

    monkeypatch.setattr(lp, "_run_check_split", _spy)
    monkeypatch.setattr(lp, "_manifest_present", lambda *a, **k: True)
    lp._lint_files(str(tmp_path), ["a.vue"])
    assert any("eslint" in c for c in seen), (
        f"`.vue` 没走进 js_ts lint 链（eslint 未被调用）⇒ lint 面对 .vue 零覆盖: {seen}")


def test_m1_mts_reaches_the_tsc_arm(tmp_path, monkeypatch):
    """★A3-M1 的接线锁★ `.mts` 必须真的进 tsc 臂（不是"派生函数含它"就算）。

    同上：初版只断派生集，"compile 触发集换回手抄小表"的突变全绿。
    现驱动 `_compile_files`，spy 命令执行器，断 npm 工程里改 `.mts` 会调起 tsc。
    """
    (tmp_path / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"noEmit":true}}',
                                            encoding="utf-8")
    (tmp_path / "a.mts").write_text("export const x: number = 1;\n", encoding="utf-8")

    seen: list[str] = []

    def _spy(cmd, project_path, timeout=60):
        seen.append(str(cmd))
        return 0, "", ""            # tsc 判通过

    monkeypatch.setattr(lp, "_run_check_split", _spy)
    monkeypatch.setattr(lp, "_manifest_present", lambda *a, **k: True)
    ok, msg = lp._compile_files(str(tmp_path), ["a.mts"], details={})
    assert any("tsc" in c for c in seen), (
        f"`.mts` 没进 tsc 臂 ⇒ 该后缀在 npm 工程上仍零类型闸: {seen}")
    assert ok, f"tsc 判通过却整体判失败: {msg[:120]}"


def test_m1_cts_reaches_the_tsc_arm(tmp_path, monkeypatch):
    """`.cts` 同款（两个后缀分开锁：只补一个是半落地）。"""
    (tmp_path / "package.json").write_text('{"name":"p"}', encoding="utf-8")
    (tmp_path / "a.cts").write_text("export const y: string = 'a';\n", encoding="utf-8")
    seen: list[str] = []
    monkeypatch.setattr(lp, "_run_check_split",
                        lambda cmd, pp, timeout=60: (seen.append(str(cmd)), 0, "", "")[1:])
    monkeypatch.setattr(lp, "_manifest_present", lambda *a, **k: True)
    lp._compile_files(str(tmp_path), ["a.cts"], details={})
    assert any("tsc" in c for c in seen), f"`.cts` 没进 tsc 臂: {seen}"


def test_m1_npx_tsc_missing_banner_is_infra_not_compile_failure():
    """★我的治法差点放大一个既有冤杀★

    npx 在 tsc 解析不到真编译器时打专属横幅；治前 `_is_infra_failure` 与 `_tool_missing`
    **都不认**它 ⇒ tsc 臂的 `elif rc != 0` 把它判成**编译不过** ⇒ npm 工程未装 typescript
    时语法完全合法的 `.js` 被判失败（既有冤杀，`.js` 早在触发集里）。
    A3-M1 把 `.mjs/.cjs/.mts/.cts` 也纳入触发集，不同批治这条就是给该冤杀**扩面**。
    """
    _banner = ("This is not the tsc command you are looking for\n"
               "To get access to the TypeScript compiler, tsc, from the command line either:")
    assert lp._is_infra_failure(_banner) is True, \
        "npx 缺 typescript 横幅未判 infra ⇒ 合法 JS/TS 会被判编译失败（冤杀）"


def test_m1_infra_marker_does_not_swallow_real_compile_errors():
    """反向锁：真编译错绝不能被新 marker 吞掉（方向不得反转）。"""
    for _t in ("error TS2304: Cannot find name 'foo'",
               "SyntaxError: Unexpected identifier",
               "AssertionError: expected 1 got 2"):
        assert lp._is_infra_failure(_t) is False, f"真失败被判 infra: {_t}"


# ───────────────── A3-M2：_per_file 截断记账 ─────────────────

def test_m2_per_file_truncation_lands_coverage_capped():
    """★核心锁★ 逐文件检查命令的截断必须落 `coverage_capped`（治前零机读键零 WARNING）。"""
    _prev = os.environ.get("SWARM_WORKER_L1_MAX_FILES")
    os.environ["SWARM_WORKER_L1_MAX_FILES"] = "20"
    try:
        d: dict = {}
        cmd = lp._derive_full_build_command(
            "/tmp", [f"a{i}.py" for i in range(120)],
            {"build": "pip", "lang": "python"}, details=d)
        assert cmd, "夹具没派生出命令（锁会空转）"
        assert d.get("coverage_capped"), \
            "截断未落 coverage_capped ⇒ 第 cap+1 个文件起零编译却 PASS，终态账看不见"
        _entry = next(iter(d["coverage_capped"].values()))
        assert _entry["total"] == 120 and _entry["checked"] == 20
    finally:
        if _prev is None:
            os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)
        else:
            os.environ["SWARM_WORKER_L1_MAX_FILES"] = _prev


def test_m2_cap_is_the_single_source_not_hardcoded_100():
    """★两个上限并存本身是漂移种子★ 上限必须听 `SWARM_WORKER_L1_MAX_FILES`。

    治前写死 `files[:100]`：调了 env 的人以为限住了，这条路上仍是 100。
    """
    _prev = os.environ.get("SWARM_WORKER_L1_MAX_FILES")
    os.environ["SWARM_WORKER_L1_MAX_FILES"] = "7"
    try:
        d: dict = {}
        cmd = lp._derive_full_build_command(
            "/tmp", [f"a{i}.py" for i in range(50)],
            {"build": "pip", "lang": "python"}, details=d)
        assert cmd.count(".py") == 7, \
            f"上限未跟随 SWARM_WORKER_L1_MAX_FILES（命令里 {cmd.count('.py')} 个文件）"
    finally:
        if _prev is None:
            os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)
        else:
            os.environ["SWARM_WORKER_L1_MAX_FILES"] = _prev


def test_m2_no_truncation_no_account():
    """区分力锁：未超上限时不得落账（否则 coverage_capped 零区分力）。"""
    d: dict = {}
    lp._derive_full_build_command(
        "/tmp", ["a.py", "b.py"], {"build": "pip", "lang": "python"}, details=d)
    assert not d.get("coverage_capped")


def test_m2_l1_callsite_passes_details_through():
    """★A3-M2 的接线锁（中间那一跳）★ L1 调用点必须把 `details` 透传给派生器。

    初版我只驱动了 `_derive_full_build_command(..., details=d)` 本身 ⇒ "调用点不透传"的
    突变**全绿**：函数会记账，但生产上没人给它 details ⇒ 账永远是空的。
    这就是批 C 的 c4 教训（只锁两端、漏中间那一跳）在本批的复发。
    锁法与 A3-L1 的 status_out 同款：AST 数实参（不是子串搜——注释里出现 `details` 不算）。
    """
    import ast
    import pathlib

    _src = pathlib.Path(lp.__file__).read_text(encoding="utf-8")
    _calls = [n for n in ast.walk(ast.parse(_src))
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", "") == "_derive_full_build_command"]
    assert _calls, "找不到 _derive_full_build_command 的调用点（锁需重写）"
    _without = [c for c in _calls if not any(k.arg == "details" for k in c.keywords)]
    assert not _without, (
        f"{len(_without)} 个 `_derive_full_build_command` 调用点未透传 details ⇒ "
        "逐文件检查命令的截断在生产上永远不记账（coverage_capped 恒缺席）")


def test_m2_missing_details_still_warns(caplog):
    """未透传 details 的调用方（老/新）截断时必须留痕——绝不静默。"""
    _prev = os.environ.get("SWARM_WORKER_L1_MAX_FILES")
    os.environ["SWARM_WORKER_L1_MAX_FILES"] = "3"
    try:
        with caplog.at_level("WARNING"):
            lp._derive_full_build_command(
                "/tmp", [f"a{i}.py" for i in range(20)], {"build": "pip", "lang": "python"})
        assert any("A3-M2" in r.getMessage() for r in caplog.records), \
            "未透传 details 时截断无 WARNING ⇒ 完全静默"
    finally:
        if _prev is None:
            os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)
        else:
            os.environ["SWARM_WORKER_L1_MAX_FILES"] = _prev


# ───────────────── A3-M3：清单探针失败可辨 ─────────────────

def test_m3_probe_failure_is_recorded(caplog):
    """★核心锁★ 探针失败必须留 WARNING + 机读账（治前两者皆无）。"""
    lp._MANIFEST_PROBE_ERRORS.clear()
    with caplog.at_level("WARNING"):
        lp._note_manifest_probe_error(("package.json",), "exit_code=1")
    assert lp._MANIFEST_PROBE_ERRORS.get("package.json") == "exit_code=1"
    assert any("A3-M3" in r.getMessage() for r in caplog.records)
    lp._MANIFEST_PROBE_ERRORS.clear()


def test_m3_failed_probe_result_is_not_cached_as_absent(monkeypatch):
    """★真正可达的那条路★：`run_command` 返回 success=False 时不得当成"清单不存在"缓存。

    ★报告指的是 `except` 臂，而它近乎不可达★（`worker/sandbox.py:run_command` 全函数
    0 条 raise，失败一律返回 `CodeResult(success=False, error="exit_code=N")`）。
    真正可达的是"只读 cr.stdout 不看 cr.success"。本锁驱动的正是这条路。
    """
    lp._MANIFEST_PROBE_ERRORS.clear()
    lp._invalidate_manifest_cache()

    class _FakeCR:
        stdout = ""
        stderr = "find: permission denied"
        success = False
        error = "exit_code=1"

    class _FakeMgr:
        def run_command(self, sandbox, cmd, timeout=20):
            return _FakeCR()

    monkeypatch.setattr(lp, "_sandbox_ctx",
                        lambda: (object(), _FakeMgr(), "/remote"))
    got = lp._manifest_present(("package.json",), "/tmp/whatever")
    assert got is False, "保守 False 的方向不变（本条不改判定，只要求成账）"
    assert lp._MANIFEST_PROBE_ERRORS, \
        "探针失败未成账 ⇒ 与'清单真不在'同形 ⇒ compile/lint 闸静默消失且无人知道"
    # 且绝不缓存（下次必须重探）
    assert not lp._MANIFEST_PRESENT_CACHE, "失败结果被缓存 ⇒ 整个 run 都读到错的 False"
    lp._MANIFEST_PROBE_ERRORS.clear()


def test_m3_successful_probe_is_cached_and_not_accounted(monkeypatch):
    """区分力锁：探针成功时不得落失败账，且正常缓存（否则账零区分力 + 丢缓存收益）。"""
    lp._MANIFEST_PROBE_ERRORS.clear()
    lp._invalidate_manifest_cache()

    class _OKCR:
        stdout = "/remote/package.json\n"
        stderr = ""
        success = True
        error = None

    class _OKMgr:
        def run_command(self, sandbox, cmd, timeout=20):
            return _OKCR()

    monkeypatch.setattr(lp, "_sandbox_ctx", lambda: (object(), _OKMgr(), "/remote"))
    assert lp._manifest_present(("package.json",), "/tmp/whatever") is True
    assert not lp._MANIFEST_PROBE_ERRORS, "探针成功却落了失败账"
    assert lp._MANIFEST_PRESENT_CACHE, "成功结果未缓存（丢 D57 缓存收益）"
    lp._MANIFEST_PROBE_ERRORS.clear()
    lp._invalidate_manifest_cache()


def test_m3_reason_is_in_needs_review_enum():
    """账要接既有 needs_review 通道 ⇒ reason 必须在共享枚举里（写侧/消费侧同源）。"""
    from swarm.types import NEEDS_REVIEW_REASONS

    assert "manifest_probe_failed" in NEEDS_REVIEW_REASONS
    assert "ts_gate_unavailable" in NEEDS_REVIEW_REASONS


# ───────────────── A3-M4：infra 归因成账（不改判） ─────────────────

@pytest.mark.parametrize("text,marker_net,loopback", [
    ("curl: (7) Failed to connect to localhost port 8080 after 0 ms: Connection refused",
     True, True),
    ("Error: connect ECONNREFUSED 127.0.0.1:5432", True, True),
    ("dial tcp 10.0.0.5:5432: connect: connection refused", True, False),
    ("could not resolve host: registry.npmjs.org", True, False),
    ("no space left on device", False, False),
    ("sh: 1: mvn: not found", False, False),
])
def test_m4_attribution_shape(text, marker_net, loopback):
    """归因账必须给出 marker + 是否网络族 + 目标是否回环。"""
    attr = lp._infra_attribution(text)
    assert attr is not None, f"未成账: {text[:50]}"
    assert attr["network_family"] is marker_net
    assert attr["loopback_target"] is loopback


def test_m4_verdict_is_unchanged():
    """★刻意不改判★ 归因是加账，不是改极性。

    据回环翻转会把沙箱自带本机服务（本地 PG/Redis/docker daemon）真挂时的**真 infra**
    打回 capability ⇒ 硬 FAIL 取代应有重试 ⇒ 正是 finding 自己警告的方向，也是 DR-04-F7
    撤销过的那类改动。本条锁住"判定不变"，防后续有人顺手翻转。
    """
    _loopback_infra = "Error: connect ECONNREFUSED 127.0.0.1:5432"
    assert lp._is_infra_failure(_loopback_infra) is True, \
        "回环目标被翻转成非 infra ⇒ 真 infra（沙箱本机服务挂）会被打成 capability 硬 FAIL"


def test_m4_non_infra_has_no_attribution():
    """非 infra 输出必须返 None（账不得对一切输入都非空）。"""
    assert lp._infra_attribution("AssertionError: boom") is None
    assert lp._infra_attribution("") is None


def test_m4_network_marker_family_derives_from_the_main_table():
    """网络族子集必须是主表的**子集**——否则子集里的条目永远不会被命中。

    「为漏项造的兜底网不能与主判据表脱节」的同型检查。
    """
    _main = set(lp._LINT_INFRA_MARKERS)
    _net = set(lp._NETWORK_INFRA_MARKERS)
    assert _net <= _main, f"网络族有主表之外的条目（恒不命中）: {_net - _main}"


# ───────────────── A3-M5 / A3-L1 / A3-L2：三条降级账 ─────────────────

def test_m5_reconcile_returns_error_account():
    """A3-M5：单生态异常必须回传机读账（治前 debug 吞掉，与"无需补注册"同形）。"""
    import swarm.worker.workspace_manifest as wm

    def _boom(root, hint):
        raise OSError("pom 读取失败")

    _orig = wm._RECONCILE_DISPATCH
    try:
        wm._RECONCILE_DISPATCH = (_boom,)
        out = wm._reconcile_manifests_unlocked("/tmp", None, False)
    finally:
        wm._RECONCILE_DISPATCH = _orig
    assert "reconcile_errors" in out, "返回值缺 reconcile_errors ⇒ 挂了与无需补不可分"
    assert "_boom" in out["reconcile_errors"]
    assert "OSError" in out["reconcile_errors"]["_boom"]


def test_m5_reconcile_error_is_warning_not_debug(caplog):
    """★升级到 WARNING★：默认日志级别是 INFO，debug 在生产上根本不可见。"""
    import swarm.worker.workspace_manifest as wm

    def _boom(root, hint):
        raise OSError("boom")

    _orig = wm._RECONCILE_DISPATCH
    try:
        wm._RECONCILE_DISPATCH = (_boom,)
        with caplog.at_level("WARNING"):
            wm._reconcile_manifests_unlocked("/tmp", None, False)
    finally:
        wm._RECONCILE_DISPATCH = _orig
    assert any("A3-M5" in r.getMessage() for r in caplog.records), \
        "逐生态异常仍是 debug ⇒ 生产不可见"


def test_m5_no_error_means_empty_account():
    """区分力锁：无异常时账为空 dict（形状一致，消费侧无需判键在不在）。"""
    import swarm.worker.workspace_manifest as wm

    _orig = wm._RECONCILE_DISPATCH
    try:
        wm._RECONCILE_DISPATCH = (lambda root, hint: ([], {}),)
        out = wm._reconcile_manifests_unlocked("/tmp", None, False)
    finally:
        wm._RECONCILE_DISPATCH = _orig
    assert out.get("reconcile_errors") == {}


def test_l1_push_status_out_is_passed_at_module_reg_callsite():
    """★A3-L1 接线锁★ module-reg 调用点必须传 `status_out`。

    W-22 造这个原语正是为区分【无沙箱=本地模式】与【有沙箱但推送未达】，而它此前只被
    A2 调用点消费。本调用点返 0 时两态合并不可分。
    锁法：spy `_push_manifests_to_sandbox`，断它**收到了** status_out 实参（不断源码文本）。
    """
    import inspect

    _sig = inspect.signature(lp._push_manifests_to_sandbox)
    assert "status_out" in _sig.parameters, "原语签名变了，本锁需重写"

    # 驱动不了整条 L1（要沙箱），故断"两个调用点都带 status_out 关键字"这一接线事实：
    # 用 AST 数实参，而不是子串搜（注释里出现 status_out 不算）。
    import ast
    import pathlib

    _src = pathlib.Path(lp.__file__).read_text(encoding="utf-8")
    _tree = ast.parse(_src)
    _calls = [n for n in ast.walk(_tree)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", "") == "_push_manifests_to_sandbox"]
    assert len(_calls) >= 2, f"调用点少于 2 个（A2 + module-reg），实得 {len(_calls)}"
    _without = [c for c in _calls
                if not any(k.arg == "status_out" for k in c.keywords)]
    assert not _without, (
        f"{len(_without)} 个 `_push_manifests_to_sandbox` 调用点未传 status_out ⇒ "
        "推送未达与无沙箱两态合并，brain 会拿 transient 失败去做无效重排")


def test_l2_pkg_decl_error_is_machine_readable(caplog):
    """A3-L2：包声明对账异常必须成账（治前 debug + 返回部分结果，半死不可辨）。"""
    lp._PKG_DECL_CHECK_ERROR.clear()

    import swarm.project.diff_apply as da
    _orig = da.split_diff_by_file
    try:
        def _boom(_d):
            raise ValueError("正则表崩了")
        da.split_diff_by_file = _boom  # type: ignore[assignment]
        with caplog.at_level("WARNING"):
            out = lp._package_decl_mismatches("dummy diff")
    finally:
        da.split_diff_by_file = _orig  # type: ignore[assignment]

    assert out == [], "异常时返回部分结果（此例为空）——形状不变"
    assert lp._PKG_DECL_CHECK_ERROR.get("error"), \
        "异常未成账 ⇒ '闸跑完了没不符'与'闸跑到一半炸了'不可分"
    assert any("A3-L2" in r.getMessage() for r in caplog.records)
    lp._PKG_DECL_CHECK_ERROR.clear()


def test_l2_ok_flag_cannot_carry_the_gate_health_fact():
    """★为什么必须单列一个键★ `l1_1b_package_decl_ok` 在异常中断时**同样为真**。

    部分结果为空即"没不符" ⇒ 那个布尔承载不了"闸跑完了吗"这件事实。
    """
    lp._PKG_DECL_CHECK_ERROR.clear()
    import swarm.project.diff_apply as da
    _orig = da.split_diff_by_file
    try:
        def _boom(_d):
            raise ValueError("boom")
        da.split_diff_by_file = _boom  # type: ignore[assignment]
        _mis = lp._package_decl_mismatches("d")
    finally:
        da.split_diff_by_file = _orig  # type: ignore[assignment]
    # 这正是治前的假象：ok=True 而闸其实炸了
    assert (not _mis) is True
    assert lp._PKG_DECL_CHECK_ERROR, "必须有独立键承载'闸炸了'"
    lp._PKG_DECL_CHECK_ERROR.clear()
