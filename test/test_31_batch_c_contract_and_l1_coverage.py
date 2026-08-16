"""31 号文批C：契约/L1 覆盖面两条（A2-H1 静默删账 / A3-H1 JS 语法闸零覆盖）。

两条都不是"闸判错了"，而是**闸的覆盖面/出口面有结构性缺口**：

- **A2-H1（静默删需求）**：`normalize_plan_scopes` 规则5 的 `_sole_owner` 归并把 N 个逻辑
  契约模块的依赖要求全挂到同一 owner（N:1），而 `_inject_templates_into_pom_owners` 按契约
  条目 1:1 生成模板 ⇒ 同一 owner 上出现【1 份只含单模块 artifacts 的模板】+【N 条规则5 行】
  ⇒ `reconcile_template_exam` 信"模板即真值"把其余 N-1 条**静默删除**。
  两层危害：① 验收面不再要求那些依赖；② **模板本身就不全**，它会被 worker「原样写入」pom
  ⇒ 编译期缺依赖，且归因指向"worker 漏写依赖"（找错人）。
  按仓内纪律**静默丢需求比矛盾考卷更坏**。
- **A3-H1（零覆盖）**：`.js` 在非 npm 工程上 compile/lint/build **三面同时缺席**，且 st-10
  静态资源放行分支的 `_has_compilable` 后缀集不含 `.js` ⇒ 纯 `.js` 子任务恒判"无可编译源"
  ⇒ 一路 L1 PASS。坏 JS 只在浏览器端炸：L2 不编译静态资源、runtime smoke 起 Spring Boot
  照样成功 ⇒ 执行期全链零信号。Maven+Thymeleaf 正是本仓 E2E 基线形态。

突变判据（逐个跑、每次 git status、突变后清 pyc）：
1. 取消模板 artifacts 并集 ⇒ A2-H1 并集锁红。
2. 把 `acceptance_dropped` 记账删掉 ⇒ A2-H1 账锁红。
3. 删 `node --check` 臂 ⇒ A3-H1 全组红。
4. `_has_compilable` 去掉 `_JS_SYNTAX_EXTS` ⇒ st-10 入口锁红。
5. 把 `_tsc_verdict` 判据换回"有 package.json" ⇒ tsc infra 跳过那条锁红。
"""
from __future__ import annotations

import re
import subprocess

import pytest

import swarm.brain.contract_utils as cu
from swarm.types import FileScope, SubTask, TaskPlan
from swarm.worker.l1_pipeline import _JS_SYNTAX_EXTS, _compile_files

_R5 = re.compile(r"依赖: \[")

_CONTRACT = {
    "dependencies": [
        {"module": "app", "artifacts": ["org.springframework.boot:spring-boot-starter-web:3.2.0"]},
        {"module": "alarm-robot", "artifacts": ["cn.hutool:hutool-all:5.8.25",
                                               "com.squareup.okhttp3:okhttp:4.12.0"]},
        {"module": "alarm-template", "artifacts": ["org.freemarker:freemarker:2.3.32"]},
    ],
}


# ═════════════════════ A2-H1 ═════════════════════

def _maven_repo(tmp_path):
    """单物理模块 Maven 工程（app/pom.xml 是唯一清单 owner）+ 真 git 仓。

    ★夹具必须是真 git 仓 + 真模板产地格式★：模板由 `_extract_auth_templates` 按
    `【权威 … 模板（… 原样写入 <path>）】` + 围栏抽取，措辞不对就整段不执行——
    施治期我第一版探针正因为手写了别的措辞而"没复现"，误以为 finding 不成立。
    """
    (tmp_path / "app/src/main/java/com/x").mkdir(parents=True)
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.x</groupId><artifactId>root</artifactId>"
        "<version>1.0</version></project>\n", encoding="utf-8")
    run = lambda *a: subprocess.run(a, cwd=tmp_path, capture_output=True,  # noqa: E731
                                    text=True, check=True)
    run("git", "init", "-q")
    run("git", "add", "-A")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return str(tmp_path)


def _plan_single_module():
    plan = TaskPlan(subtasks=[SubTask(
        id="st-1", description="搭建 app 模块骨架并实现全部逻辑模块的代码",
        scope=FileScope(create_files=["app/pom.xml", "app/src/main/java/com/x/App.java"]),
        acceptance_criteria=["编译通过"])], parallel_groups=[["st-1"]])
    plan.shared_contract = _CONTRACT
    return plan


def _r5_lines(plan):
    return [a for a in (plan.subtasks[0].acceptance_criteria or []) if _R5.search(a)]


def _arts_of(lines) -> set[str]:
    got: set[str] = set()
    for a in lines:
        got |= set(re.findall(r"[\w.\-]+:([\w.\-]+):[\w.\-]+", a))
        got |= {m.strip("'\" ") for m in re.findall(r"'([^']+)'", a)}
    return {g for g in got if g and ":" not in g}


def test_a2h1_template_takes_artifacts_union(tmp_path):
    """★根因锁★ 多个逻辑模块塌进单物理模块时，权威模板必须含**全部** artifacts。

    治前模板只含 `app` 一个模块的 artifacts ⇒ 它被 worker 原样写入 pom ⇒ 编译期缺
    hutool/okhttp/freemarker。只修"验收面别被删"是不够的——pom 照样是错的。
    """
    proj = _maven_repo(tmp_path)
    plan = _plan_single_module()
    cu.normalize_plan_scopes(plan)
    cu._inject_templates_into_pom_owners(plan, proj, None)

    desc = plan.subtasks[0].description or ""
    for art in ("spring-boot-starter-web", "hutool-all", "okhttp", "freemarker"):
        assert art in desc, (
            f"权威模板必须含 {art}（逻辑模块归并进单物理模块 ⇒ 模板取并集）；"
            "模板不全 ⇒ 原样写入即缺依赖")


def test_a2h1_no_dependency_requirement_is_silently_lost(tmp_path):
    """端到端：reconcile 后规则5 行覆盖的 artifacts 集合**不得缩小**。

    判据是 artifacts 覆盖而不是行数——多条合法坍缩成一条是**允许**的（治后那一条覆盖
    全部 artifacts）；不允许的是覆盖面变小。
    """
    proj = _maven_repo(tmp_path)
    plan = _plan_single_module()
    cu.normalize_plan_scopes(plan)
    before = _arts_of(_r5_lines(plan))
    assert len(before) == 4, f"前提：normalize 应产出 4 个 artifacts 的要求，实得 {before}"

    cu._inject_templates_into_pom_owners(plan, proj, None)
    cu.reconcile_template_exam(plan)

    after = _arts_of(_r5_lines(plan))
    lost = before - after
    assert not lost, (
        f"reconcile 静默吞掉了依赖要求 {sorted(lost)}——按仓内纪律静默丢需求比矛盾考卷更坏"
        "（L1/L2 编译期才炸，且归因指向 worker 漏写依赖）")


def test_a2h1_dropped_requirements_are_machine_readable(tmp_path, monkeypatch):
    """★第二道网★ 并集取证 fail-open 时（模板退回只含单模块 artifacts），
    被删的规则5 行必须进 `acceptance_dropped` 机读键 —— 治前只有一个
    `acceptance_rewritten` 计数、日志不列内容 ⇒ "少了哪条依赖要求"只能靠考古。
    """
    proj = _maven_repo(tmp_path)
    plan = _plan_single_module()
    cu.normalize_plan_scopes(plan)

    # 模拟并集取证失败（fail-open 分支）
    monkeypatch.setattr(cu, "_module_manifest_owners",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated")))
    cu._inject_templates_into_pom_owners(plan, proj, None)
    monkeypatch.undo()

    rec = cu.reconcile_template_exam(plan)
    r = (rec or {}).get("st-1") or {}
    dropped = r.get("acceptance_dropped") or []
    assert dropped, (
        "并集 fail-open 时真丢失必须成账（第二道网）；账为空＝这一类丢失又变回只能靠考古")
    joined = " ".join(dropped)
    assert "hutool" in joined or "freemarker" in joined, (
        f"账必须列出被删的原行内容（不能只记条数），实得 {dropped}")


def test_a2h1_ledger_is_empty_when_nothing_lost(tmp_path):
    """★区分力★ 正常路径（模板取了并集）下账必须为空——否则这个账每轮都非空，
    使用者学会忽略它 ⇒ 等于没有账。但键本身必须在（缺席≠没丢东西）。"""
    proj = _maven_repo(tmp_path)
    plan = _plan_single_module()
    cu.normalize_plan_scopes(plan)
    cu._inject_templates_into_pom_owners(plan, proj, None)

    rec = cu.reconcile_template_exam(plan)
    r = (rec or {}).get("st-1") or {}
    assert "acceptance_dropped" in r, "机读键必须预声明（键缺失与'没丢东西'不可分）"
    assert r["acceptance_dropped"] == [], (
        f"并集生效后不该有真丢失，实得 {r['acceptance_dropped']}——"
        "按'行文本不等'记账会让账每轮都非空（狼来了）")


def test_a2h1_ledger_reaches_state_and_progress():
    """★新账必须有消费者（血规 10④）★ 接线锁：声明 → reducer 策略 → 节点透传 → progress 读点。
    LangGraph 对**未在 schema 声明**的键会静默丢弃，所以这四处缺一账就到不了机读面。"""
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
    assert "exam_rule5_dropped" in BrainState.__annotations__, (
        "BrainState 未声明 ⇒ LangGraph 静默丢弃该键（本仓已立档的坑）")
    assert ACCOUNTING_KEY_LIFECYCLE.get("exam_rule5_dropped") == "round", (
        "必须是 last-write-wins（无命中={} 不粘滞），绝不进 append-only degraded_reasons")


def test_a2h1_finisher_collects_ledger_into_out(monkeypatch):
    """★接线锁：plan_finisher 必须把账收进 `out`★

    治前我的锁只覆盖了两端（reconcile 产账 / progress 读账），**中间那一跳没测**——
    c4 突变（把 `exam_dropped_out=_exam_dropped` 实参摘掉）实测**仍绿**：账在
    reconcile 里生成、在 progress 里能读，但 finisher 不收集 ⇒ 生产上永远是空的。
    这正是「加机制先数调用点，一个不落地列出来」——生产点/消费点之间的每一跳都要有锁。

    行为锁：把 `inject_build_scaffold_subtasks` 换成间谍，断言 finisher 传了该 out 参数
    并把结果写进 `out["exam_rule5_dropped"]`。
    """
    import swarm.brain.plan_finisher as pf

    # ★patch 必须打在【定义模块】上★：finisher 里是函数内 `from ... import`，
    # 打在 plan_finisher 上会 AttributeError（实测），打在 re-export 上则是 vacuous 绿。
    _seen: dict = {}

    def _fake_inject(plan, project_path=None, file_plan=None,
                     unverified_out=None, exam_dropped_out=None):
        _seen["got_param"] = exam_dropped_out is not None
        if exam_dropped_out is not None:
            exam_dropped_out["st-9"] = ["本模块 pom.xml 必须声明 alarm-robot 所需依赖: [...]"]
        return []

    monkeypatch.setattr(cu, "inject_build_scaffold_subtasks", _fake_inject)

    plan = TaskPlan(subtasks=[SubTask(
        id="st-1", description="d", scope=FileScope(create_files=["app/pom.xml"]),
        acceptance_criteria=["编译通过"])], parallel_groups=[["st-1"]])

    out = pf.finish_plan_deterministic(plan, [], task_description="d") or {}

    assert _seen.get("got_param") is True, (
        "finisher 必须传 exam_dropped_out——不传则账在生产上永远为空（c4 突变实测仍绿的那条缺口）")
    assert out.get("exam_rule5_dropped") == {
        "st-9": ["本模块 pom.xml 必须声明 alarm-robot 所需依赖: [...]"]}, (
        f"必须把账写进 out（进而 always-emit 到 state），实得 {out.get('exam_rule5_dropped')!r}")


def test_a2h1_ledger_surfaces_in_progress_endpoint(monkeypatch):
    """★行为锁（非 getsource）★ progress 端点是唯一权威机读出口（纪律 #106：绝不解析
    swarm.log）。把 checkpoint state 造成含该账，断言它真出现在 progress 输出里。

    比断源码含某字符串强：重命名/换实现不误红，而真断开（谁把这行删了）必红。
    """
    import asyncio

    from swarm.brain import runner as _runner

    _state = {
        "dispatch_remaining": [],
        "subtask_results": {},
        "failed_subtask_ids": [],
        "exam_rule5_dropped": {"st-1": ["本模块 pom.xml 必须声明 alarm-robot 所需依赖: [...]"]},
    }

    class _Snap:
        values = _state

    class _Graph:
        async def aget_state(self, _cfg):
            return _Snap()

    monkeypatch.setattr(_runner, "get_compiled_brain_graph", lambda: _Graph())
    monkeypatch.setattr(_runner.store, "get_task", lambda _t: {"thread_id": "t", "project_id": "p"})

    out = asyncio.run(_runner.get_task_progress("task-1")) or {}

    assert "exam_rule5_dropped" in out, (
        "progress 必须透出该账——没有读点＝新账没有消费者＝没造（血规 10④）")
    assert out["exam_rule5_dropped"] == _state["exam_rule5_dropped"], (
        f"账内容必须原样透出，实得 {out.get('exam_rule5_dropped')}")


def test_a2h1_ledger_always_emits_empty_dict(monkeypatch):
    """always-emit 区分力：state 里没有该键时 progress 也要发 {}——让"本轮没删东西"
    与"这版代码还没这个账"可区分（缺席不可辨是本仓已立档族）。"""
    import asyncio

    from swarm.brain import runner as _runner

    class _Snap:
        values = {"dispatch_remaining": [], "subtask_results": {}, "failed_subtask_ids": []}

    class _Graph:
        async def aget_state(self, _cfg):
            return _Snap()

    monkeypatch.setattr(_runner, "get_compiled_brain_graph", lambda: _Graph())
    monkeypatch.setattr(_runner.store, "get_task", lambda _t: {"thread_id": "t", "project_id": "p"})

    out = asyncio.run(_runner.get_task_progress("task-1")) or {}
    assert out.get("exam_rule5_dropped") == {}, (
        f"无命中也必须发 {{}}（always-emit），实得 {out.get('exam_rule5_dropped')!r}")


# ═════════════════════ A3-H1 ═════════════════════

_BAD_JS = "functon broken(){retrun 1;}\n"
_GOOD_JS = "function ok(){return 1;}\n"


def _maven_static_project(tmp_path, files: dict[str, str]) -> str:
    """Maven 单体（有 pom、**无 package.json**）+ Thymeleaf/admin 静态资源。

    ★夹具形状就是命题本身★：本 finding 的整个触发前提是"无 package.json"——
    加一个 package.json 就变成测 tsc 路径（另一条命题）。
    """
    (tmp_path / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>\n", encoding="utf-8")
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return str(tmp_path)


@pytest.mark.parametrize("ext", list(_JS_SYNTAX_EXTS))
def test_a3h1_broken_js_fails_compile_gate_without_package_json(tmp_path, ext):
    """★病灶本尊★ Maven 工程（无 package.json）里语法坏的 JS 必须判编译不过。

    治前三面同时缺席：`_compile_files` 的 js_ts 段被 `_manifest_present(package.json)`
    门控整段跳过 ⇒ 直落 `return True, "compile ok"`；`_lint_files` 被 eslintrc 门控跳过；
    `_derive_full_build_command` 对 .js 臂返 ''。而坏 JS 只在浏览器端炸：L2 不编译静态
    资源、runtime smoke 起 Spring Boot 照样成功 ⇒ 执行期全链零信号。
    """
    if not __import__("shutil").which("node"):
        pytest.skip("本机无 node（闸按 infra 口径跳过，已落机读键，另有锁覆盖）")
    rel = f"src/main/resources/static/app{ext}"
    proj = _maven_static_project(tmp_path, {rel: _BAD_JS})
    details: dict = {}
    ok, msg = _compile_files(proj, [rel], details=details)
    assert ok is False, f"{ext} 语法错必须判编译不过（治前恒返 compile ok）"
    assert details.get("js_syntax_failed") == rel, (
        f"失败必须落机读键指向具体文件，实得 {details}")


@pytest.mark.parametrize("ext", list(_JS_SYNTAX_EXTS))
def test_a3h1_valid_js_passes_and_leaves_positive_trace(tmp_path, ext):
    """区分力 + 正面留痕：合法 JS 必须过（否则把闸拧成恒失败也能让上面那组绿），
    且要留 `js_syntax_checked` —— 让"闸跑过且全过"与"闸没跑"可机读区分。"""
    if not __import__("shutil").which("node"):
        pytest.skip("本机无 node")
    rel = f"src/main/resources/static/ok{ext}"
    proj = _maven_static_project(tmp_path, {rel: _GOOD_JS})
    details: dict = {}
    ok, _ = _compile_files(proj, [rel], details=details)
    assert ok is True, f"合法 {ext} 不得判失败（冤杀）"
    assert rel in (details.get("js_syntax_checked") or []), (
        f"闸跑过必须留正面痕迹（缺席不可辨），实得 {details}")


def test_a3h1_jsx_deliberately_excluded_from_node_check():
    """★冤杀边界锁（施治期实测逮到）★ `.jsx` 必须**不在** `node --check` 后缀集里。

    `node --check t.jsx` 对**任何**内容都抛 `ERR_UNKNOWN_FILE_EXTENSION` 退出 1——
    纯 JS 内容的 .jsx 也照抛（JSX 不是合法 JS，要 babel/tsc/esbuild 才能解析）。
    我初版把 .jsx 收进来了，结果每个 .jsx 都被冤判语法错，正是这条锁的兄弟用例抓出来的。
    本锁防"看起来该收就加回来"：加回去这条必红。
    """
    assert ".jsx" not in _JS_SYNTAX_EXTS, (
        "node --check 对 .jsx 恒抛 ERR_UNKNOWN_FILE_EXTENSION ⇒ 收进来＝每个 .jsx 都冤杀；"
        ".jsx 的覆盖归 tsc 臂，非 npm 工程上如实登记为缺口（宁可诚实缺席，不要冤杀）")
    assert set(_JS_SYNTAX_EXTS) == {".js", ".mjs", ".cjs"}, (
        f"后缀集漂移: {_JS_SYNTAX_EXTS}——增删必须是刻意行为并同步本断言")


@pytest.mark.parametrize("path,want,why", [
    ("src/main/resources/static/app.js", True, "★病灶本尊★ .js 现有 node --check 闸 ⇒ 是可编译源"),
    ("src/main/resources/static/m.mjs", True, ".mjs 同上"),
    ("src/main/resources/static/m.cjs", True, ".cjs 同上"),
    ("src/main/java/com/x/A.java", True, "JVM 源码，治前治后都算"),
    ("src/main/resources/static/a.css", False, "纯静态资源，无确定性闸 ⇒ 不算（st-10 放行的本意）"),
    ("src/main/resources/templates/a.html", False, "Thymeleaf 模板，同上"),
    ("src/main/resources/static/c.jsx", False, ".jsx 不在 node --check 集里（恒抛）⇒ 如实不算"),
])
def test_a3h1_has_compilable_source_counts_js(path, want, why):
    """★生产入口锁★ st-10 静态资源放行分支的门控必须把 `.js` 算作可编译源。

    治前只数 `.java/.kt/.scala/.go/.rs/.ts/.tsx/.vue` ⇒ 纯 .js 子任务在 Maven 工程里恒判
    "无可编译源" ⇒ `l1_2_1_build_ok=True` + build_skipped ⇒ 继续走 lint（也跳）⇒ 一路 PASS。

    ★本锁直接调**生产函数** `_has_compilable_source`★——初版我在测试里重建了一份后缀元组，
    于是"把生产后缀集改坏"的突变**仍绿**（c6 实测），因为测的是我自己的表达式。
    为此把内联表达式抽成了模块级具名函数：内联的东西测试消费不到，就只能照抄，
    照抄就等于给自己背书（本仓已立档的"假探针宽度替代码背书"形态）。
    """
    from swarm.worker.l1_pipeline import _has_compilable_source
    assert _has_compilable_source([path]) is want, f"{path}: 应为 {want}——{why}"


def test_a3h1_gate_runs_when_tsc_skipped_for_infra(tmp_path, monkeypatch):
    """★同一 finding 的第二个入口★ 判据必须是"tsc 真给出通过裁决"，不是"有 package.json"。

    有 package.json 但 tsc 因 infra（无 node_modules / npx 装不上 / 本机 tsc 是别的同名
    二进制）被跳过时，`.js` 会重新掉回零语法闸状态。施治期实测本机 tsc 正是别的二进制。
    """
    if not __import__("shutil").which("node"):
        pytest.skip("本机无 node")
    import swarm.worker.l1_pipeline as lp
    (tmp_path / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.js").write_text(_BAD_JS, encoding="utf-8")
    # 模拟 tsc 真 infra 跳过（_is_infra_failure 认得的形态）
    monkeypatch.setattr(lp, "_run_check_split",
                        lambda cmd, cwd, timeout=60: (
                            1, "", "npm ERR! network request to https://registry.npmjs.org "
                                   "failed, reason: getaddrinfo ENOTFOUND"))
    details: dict = {}
    ok, _ = lp._compile_files(str(tmp_path), ["src/a.js"], details=details)
    assert ok is False, (
        "tsc 因 infra 跳过后，node --check 臂必须补上——否则本 finding 换个入口原样复发")
    assert details.get("js_syntax_failed") == "src/a.js", f"实得 {details}"


def test_a3h1_missing_node_is_machine_readable(tmp_path, monkeypatch):
    """工具缺失按既有 infra 口径跳过，但**必须落机读键**（别再零留痕）。
    治前整条通道连 details 都是空字典＝闸缺席与闸通过不可分。"""
    import swarm.worker.l1_pipeline as lp
    rel = "src/main/resources/static/app.js"
    proj = _maven_static_project(tmp_path, {rel: _BAD_JS})
    monkeypatch.setattr(lp.shutil, "which", lambda _n: None)   # 模拟无 node
    details: dict = {}
    ok, _ = lp._compile_files(proj, [rel], details=details)
    assert ok is True, "工具缺失＝infra，不冤判编译失败（与既有 tsc/php 口径一致）"
    assert details.get("js_syntax_gate_skipped") == "node_missing", (
        f"跳过必须机读可辨，实得 {details}")
    assert rel in (details.get("js_syntax_files_unchecked") or []), (
        f"未检查的文件必须列出（否则'闸跳过了'无从追查），实得 {details}")
