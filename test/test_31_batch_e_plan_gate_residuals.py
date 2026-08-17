"""31 号文批 E 锁：A1-M1 注入通道漏传 punt 账 · A1-M2 剔除账无机读面 · A1-M3 禁令覆盖面收窄 · A1-L1 文档漂移。

四条的共同形态与批 D 同源：**判据/文档与实际运行的东西不一致**，且三条都是"同一不变量的
生产点/消费点没数全"（记忆已立档的复发族）。

★本文件里最该看的一条★ `test_m3_pattern_catches_family_coordinate_by_real_grep`：
A1-M3 的期望串在别处（test_round67l_plan_exam_truth）已改为**从生产单一事实源派生**，
那解决了"手抄=自己给自己背书"，但引入了反向缺口——把 `pom_dep_ban_pattern` 改回闭合标签，
派生断言会跟着变、照样全绿。故语义必须由**真 grep 行为**钉住，不能由字符串相等钉住。
"""

from __future__ import annotations

import subprocess

from swarm.brain.plan_finisher import (
    pom_dep_ban_pattern,
    sanitize_negated_grep_exam,
    wire_symbol_consumption_edges,
)
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _st(sid: str, *, create=None, verify=None, desc="", depends=None) -> SubTask:
    return SubTask(
        id=sid, description=desc or sid,
        scope=FileScope(create_files=list(create or [])),
        depends_on=list(depends or []),
        harness=TaskHarness(language="java", verify_commands=list(verify or [])),
    )


# ───────────────── A1-M3：禁令覆盖面（语义锁，非字符串锁）─────────────────

def _grep_verdict(pattern: str, content: str, tmp_path) -> bool:
    """在真 shell 里跑 `! grep -qi <pattern> <file>`，返回"负断言是否判过（放行）"。

    ★为什么必须真跑★ A1-M3 的整个命题是"改写后的 pattern 在 grep 语义下覆盖什么"。
    用字符串相等断言只能钉住形态，钉不住语义——而形态的期望值本身来自生产代码（派生），
    改坏生产就一起变。真 grep 是唯一独立于实现的判据。
    """
    f = tmp_path / "pom.xml"
    f.write_text(content, encoding="utf-8")
    rc = subprocess.run(["grep", "-qi", pattern, str(f)],
                        capture_output=True).returncode
    return rc != 0   # grep 未命中 ⇒ `! grep` 判过 ⇒ 放行


_POM_FAMILY = """<project>
  <dependencies><dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok-mapstruct-binding</artifactId>
  </dependency></dependencies>
</project>
"""

_POM_PROSE_ONLY = """<project>
  <!-- 零重依赖：不引入 lombok，全部手写 getter/setter -->
  <dependencies><dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-web</artifactId>
  </dependency></dependencies>
</project>
"""

_POM_GROUPID_ONLY = """<project>
  <dependencies><dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>mapstruct</artifactId>
  </dependency></dependencies>
</project>
"""

_POM_EXACT = """<project>
  <dependencies><dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
  </dependency></dependencies>
</project>
"""


def test_m3_pattern_catches_family_coordinate_by_real_grep(tmp_path):
    """★核心语义锁★ 改写后的禁令必须**拦下**同族坐标 `lombok-mapstruct-binding`。

    治前（闭合标签 `<artifactId>lombok</artifactId>`）：放行 ⇒ worker 引入同族坐标判过。
    突变：把 `pom_dep_ban_pattern` 改回闭合标签 ⇒ 本条红（真 grep 会放行）。
    """
    assert _grep_verdict(pom_dep_ban_pattern("lombok"), _POM_FAMILY, tmp_path) is False, \
        "同族坐标 lombok-mapstruct-binding 被放行 ⇒ 禁令覆盖面被收窄（A1-M3 未生效）"


def test_m3_pattern_still_exempts_comment_prose_by_real_grep(tmp_path):
    """★反向锁★ 必须仍放行"只在注释里提到 lombok"的 pom——这是 B3⑥ 的误杀治本，不得倒退。

    若有人把改写整块删掉退回裸子串 `lombok` ⇒ 本条红（裸子串命中注释 ⇒ 冤杀）。
    """
    assert _grep_verdict(pom_dep_ban_pattern("lombok"), _POM_PROSE_ONLY, tmp_path) is True, \
        "注释散文被判违禁 ⇒ B3⑥ 的误杀治本倒退了"


def test_m3_pattern_does_not_false_reject_groupid_only(tmp_path):
    """groupId 含词但 artifactId 不含 ⇒ 未引入该坐标本体 ⇒ 应放行（比裸子串更准的那一面）。"""
    assert _grep_verdict(pom_dep_ban_pattern("lombok"), _POM_GROUPID_ONLY, tmp_path) is True, \
        "groupId-only 命中被冤杀"


def test_m3_pattern_catches_exact_coordinate(tmp_path):
    """基础面：精确坐标当然要拦（防"把闸拧成恒放行"式突变）。"""
    assert _grep_verdict(pom_dep_ban_pattern("lombok"), _POM_EXACT, tmp_path) is False


def test_m3_bare_substring_is_the_false_reject_baseline(tmp_path):
    """前提锁：证明"裸子串会冤杀注释散文"这个前提为真。

    ★这条锁的作用是钉住【问题存在】★——若哪天 grep 行为或夹具变了让裸子串也不冤杀，
    那 B3⑥ 整条改写机制的立项理由就消失了，上面几条锁会变成在测一个不存在的问题
    （夹具形状决定被测命题，本仓已立档）。
    """
    assert _grep_verdict("lombok", _POM_PROSE_ONLY, tmp_path) is False, \
        "裸子串不再命中注释 ⇒ B3⑥ 的立项前提已不成立，本组锁需重新设计"


def test_m3_rewrite_is_wired_into_sanitize():
    """接线锁：`sanitize_negated_grep_exam` 必须真的用上单一事实源的形态。"""
    st = _st("st-1", create=["pom.xml"], verify=["! grep -qi 'lombok' pom.xml"])
    plan = TaskPlan(subtasks=[st])
    out = sanitize_negated_grep_exam(plan)
    assert "st-1" in out
    assert pom_dep_ban_pattern("lombok") in st.harness.verify_commands[0]


# ───────────────── A1-M2：剔除账的机读面与消费者 ─────────────────

def _cycle_plan(*, with_exam: bool = True) -> TaskPlan:
    """成环夹具：st-b 消费 st-a 的符号，而 st-a 依赖 st-b ⇒ 边成环 ⇒ B3④ 剔 st-b 的正断言。"""
    a = _st("st-a", create=["m/src/main/java/com/x/AlarmSender.java"],
            desc="create sender", depends=["st-b"])
    b = _st("st-b", create=["m/src/main/java/com/x/Consumer.java"],
            desc="consumer 注入 AlarmSender 并调用",
            verify=(["grep -q AlarmSender m/src/main/java/com/x/Consumer.java"]
                    if with_exam else []))
    return TaskPlan(subtasks=[a, b])


def test_m2_dropped_exam_lands_on_plan_account():
    """★核心锁★ 被剔断言必须落 plan 级机读账（治前：只有一条 WARNING，机读面全空）。"""
    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    assert plan.subtasks[1].harness.verify_commands == [], "前提：断言确实被剔了"
    assert plan.symbol_exam_dropped, "剔除账为空 ⇒ A1-M2 未生效（账仍只活在日志里）"
    assert "st-b" in plan.symbol_exam_dropped
    assert any("AlarmSender" in c for c in plan.symbol_exam_dropped["st-b"])


def test_m2_zeroed_account_is_a_separate_fact():
    """★归零必须单列★ 剔到 0 条与剔了一部分是**不同事实**，不得共用一个账。"""
    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    assert plan.symbol_exam_zeroed == ["st-b"], \
        "归零账缺失 ⇒ 无法与'剔了一部分'区分（响铃装在错的位置）"


def test_m2_partial_drop_does_not_ring_the_zero_bell():
    """区分力锁：还剩别的断言时，归零账必须为空（否则归零账零区分力）。"""
    plan = _cycle_plan()
    plan.subtasks[1].harness.verify_commands = [
        "grep -q AlarmSender m/src/main/java/com/x/Consumer.java",
        "test -f m/src/main/java/com/x/Consumer.java",   # 与成环符号无关，应保留
    ]
    wire_symbol_consumption_edges(plan)
    assert plan.symbol_exam_dropped.get("st-b"), "该被剔的那条没剔"
    assert plan.subtasks[1].harness.verify_commands, "无关断言被误剔"
    assert plan.symbol_exam_zeroed == [], \
        "还剩验收却响了归零铃 ⇒ 归零账没有区分力"


def test_m2_no_cycle_means_empty_accounts():
    """反向锁：无成环时两账都必须为空（防"账恒非空"式假信号）。"""
    a = _st("st-a", create=["m/src/main/java/com/x/AlarmSender.java"], desc="create sender")
    b = _st("st-b", create=["m/src/main/java/com/x/Consumer.java"],
            desc="consumer 注入 AlarmSender 并调用", depends=["st-a"],
            verify=["grep -q AlarmSender m/src/main/java/com/x/Consumer.java"])
    plan = TaskPlan(subtasks=[a, b])
    wire_symbol_consumption_edges(plan)
    assert plan.symbol_exam_dropped == {}
    assert plan.symbol_exam_zeroed == []
    assert plan.subtasks[1].harness.verify_commands, "无环却剔了断言"


def test_m2_finisher_hop_carries_the_account_into_out():
    """★中间那一跳★ `finish_plan_deterministic` 必须把 plan 账提进 `out`。

    ★批 C 的 c4 教训（本条存在的唯一理由）★：只锁两端（产账/读账）而漏掉中间这一跳，
    摘掉提取行仍全绿，而生产上 state 里的账永远是空的。
    """
    from swarm.brain import plan_finisher as pf

    # 用真 pipeline 跑一遍：夹具即上面的成环 plan
    # 真签名：finish_plan_deterministic(plan, file_plan, *, project_path=None, …)
    plan = _cycle_plan()
    out = pf.finish_plan_deterministic(plan, [], project_path=None)
    assert isinstance(out, dict)
    assert "symbol_exam_dropped" in out, \
        "out 里没有剔除账 ⇒ 生产/消费之间那一跳断了（state 永远收不到）"
    assert "symbol_exam_zeroed" in out, "out 里没有归零账"
    assert out["symbol_exam_dropped"].get("st-b"), f"账内容丢了: {out['symbol_exam_dropped']}"
    assert "st-b" in (out["symbol_exam_zeroed"] or [])


def test_m2_accounts_are_always_emitted_even_when_empty():
    """always-emit：无命中也必须发空值。

    缺席键会被 LangGraph 保持原值 ⇒ 上轮账粘滞进本轮 payload = 最坏形态的假信号
    （本仓 R67M2-T3 已为 plan_validation_warnings 治过同一个坑）。
    """
    from swarm.brain import plan_finisher as pf

    a = _st("st-a", create=["m/src/main/java/com/x/A.java"], desc="a")
    plan = TaskPlan(subtasks=[a])
    out = pf.finish_plan_deterministic(plan, [], project_path=None)
    assert out.get("symbol_exam_dropped") == {}, "无命中时该键必须在场且为空"
    assert out.get("symbol_exam_zeroed") == [], "无命中时该键必须在场且为空"


def test_m2_state_keys_are_declared_and_round_scoped():
    """★LangGraph 未声明键会被静默丢弃★——两键必须在 BrainState 里声明且注册 round 语义。"""
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState

    _ann = getattr(BrainState, "__annotations__", {})
    for k in ("symbol_exam_dropped", "symbol_exam_zeroed"):
        assert k in _ann, f"{k} 未在 BrainState 声明 ⇒ 会被 LangGraph 静默丢弃"
        assert ACCOUNTING_KEY_LIFECYCLE.get(k) == "round", \
            f"{k} 未注册 round 语义 ⇒ 跨轮粘滞"


def test_m2_progress_endpoint_consumes_both_accounts():
    """★新账必须有消费者（血规 10④）★ get_task_progress 是机读面的唯一权威出口。

    ★为什么这条锁必须【驱动函数】而不是 getsource★（本批实测教训 e9）：
    初版用 `inspect.getsource(...)` 断键名子串——摘掉消费行后测试**仍绿**，因为我自己写在
    那几行上方的注释里就包含 `symbol_exam_dropped` 这个词。getsource 分不清**活代码与
    注释/死代码**，这正是纪律 6 禁它的理由。现改为真调函数、断【返回值】里有键。
    """
    import asyncio

    from swarm.brain import runner

    _state = {
        "dispatch_remaining": [],
        "subtask_results": {},
        "plan": _cycle_plan(),
        "symbol_exam_dropped": {"st-b": ["grep -q AlarmSender x.java"]},
        "symbol_exam_zeroed": ["st-b"],
    }

    class _Snap:
        values = _state

    class _FakeGraph:
        async def aget_state(self, cfg):
            return _Snap()

    _orig_graph = runner.get_compiled_brain_graph
    _orig_get_task = runner.store.get_task
    try:
        runner.get_compiled_brain_graph = lambda *a, **k: _FakeGraph()  # type: ignore[assignment]
        runner.store.get_task = lambda tid: {  # type: ignore[assignment]
            "thread_id": "t1", "project_id": "p1", "description": "d"}
        res = asyncio.run(runner.get_task_progress("task-1"))
    finally:
        runner.get_compiled_brain_graph = _orig_graph  # type: ignore[assignment]
        runner.store.get_task = _orig_get_task  # type: ignore[assignment]

    assert res is not None, "progress 返回 None（夹具没走通，锁会空转）"
    assert res.get("symbol_exam_dropped") == {"st-b": ["grep -q AlarmSender x.java"]}, \
        f"剔除账未被 progress 出口透出 ⇒ 新账没有消费者＝没造: {res.get('symbol_exam_dropped')}"
    assert res.get("symbol_exam_zeroed") == ["st-b"], "归零账未被 progress 出口透出"


def test_m2_progress_always_emits_empty_accounts():
    """always-emit 的消费侧对照：state 里没有这两个账时，出口也必须发空值。

    "本轮没剔东西"与"这版代码还没这个账"必须可区分（缺席不可机读=层能死很久没人知道）。
    """
    import asyncio

    from swarm.brain import runner

    class _Snap:
        values = {"dispatch_remaining": [], "subtask_results": {}, "plan": _cycle_plan()}

    class _FakeGraph:
        async def aget_state(self, cfg):
            return _Snap()

    _orig_graph = runner.get_compiled_brain_graph
    _orig_get_task = runner.store.get_task
    try:
        runner.get_compiled_brain_graph = lambda *a, **k: _FakeGraph()  # type: ignore[assignment]
        runner.store.get_task = lambda tid: {  # type: ignore[assignment]
            "thread_id": "t1", "project_id": "p1", "description": "d"}
        res = asyncio.run(runner.get_task_progress("task-1"))
    finally:
        runner.get_compiled_brain_graph = _orig_graph  # type: ignore[assignment]
        runner.store.get_task = _orig_get_task  # type: ignore[assignment]

    assert res is not None
    assert "symbol_exam_dropped" in res and res["symbol_exam_dropped"] == {}
    assert "symbol_exam_zeroed" in res and res["symbol_exam_zeroed"] == []


def test_m2_validate_plan_folds_accounts_into_warnings():
    """第二个消费者：validate_plan 折进 plan_validation_warnings（人读文案面）。

    两个消费者不冗余：warnings 是"本轮 validate 说了什么"（注入通道刻意无 VALIDATE 节点），
    progress 是"这个任务当前状态"。

    ★同 e9 教训★：不用 getsource（我的注释里就有"验收归零"四个字，摘掉活代码仍绿）。
    改为真跑 `validate_plan`、断【返回的 warnings 列表】里两类文案都在且**可区分**。
    """
    import asyncio

    from swarm.brain import nodes as _n

    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)      # 真产账
    assert plan.symbol_exam_dropped and plan.symbol_exam_zeroed, "前提：两账已产生"

    state = {
        "plan": plan,
        "task_description": "probe",
        "plan_retry_count": 0,
        "affected_files": [],
        "symbol_exam_dropped": plan.symbol_exam_dropped,
        "symbol_exam_zeroed": plan.symbol_exam_zeroed,
    }
    res = _n.validate_plan(state)
    if asyncio.iscoroutine(res):
        res = asyncio.run(res)
    warns = res.get("plan_validation_warnings") or []
    _joined = "\n".join(map(str, warns))
    assert "st-b" in _joined, f"剔除账未折进 warnings（人读面看不见）: {warns}"
    # ★两条文案必须可区分★（不同后果不同措辞，否则响铃装在错的位置）
    _zero_lines = [w for w in warns if "验收归零" in str(w)]
    _drop_lines = [w for w in warns if "因符号消费成环被确定性剔除" in str(w)]
    assert _zero_lines, f"归零缺独立文案 ⇒ 与'剔了一部分'塌成同一条: {warns}"
    assert _drop_lines, f"缺'剔了一部分'的文案: {warns}"
    assert _zero_lines != _drop_lines, "两类文案相同 ⇒ 零区分力"


def test_m2_plan_rebuild_points_carry_the_accounts():
    """★生产/消费点必须数全★ 两个 plan 重建点都得携带新账，否则重建一轮即丢。

    B-1（21 号文）为 finisher_attached/symbol_cycle_pairs 治过同一个坑；加新 plan 级账时
    漏掉重建点就是同族第三例。此处用**行为**断言：重建后账还在。
    """
    from swarm.brain.planning_nodes import _rebuild_plan

    plan = _cycle_plan()
    wire_symbol_consumption_edges(plan)
    assert plan.symbol_exam_dropped, "前提：账已产生"
    rebuilt = _rebuild_plan(plan, list(plan.subtasks))
    assert rebuilt.symbol_exam_dropped == plan.symbol_exam_dropped, \
        "_rebuild_plan 丢了剔除账（重建一轮即丢，B-1 同族）"
    assert rebuilt.symbol_exam_zeroed == plan.symbol_exam_zeroed, \
        "_rebuild_plan 丢了归零账"


# ───────────────── A1-M1：注入通道的 punt 账 ─────────────────

def test_m1_inject_channel_passes_layout_punted():
    """★核心接线锁★ 注入通道调 C1 时必须传 `layout_punted`。

    治前：缺省 None ⇒ `_punted_set` 空 ⇒ "不占无主宽容直接打回"那条 `result.add` 永不触发
    ⇒ punt 符号只参与 ratio>0.4 比率判定 ⇒ 占比小就放行直穿 DISPATCH。
    本通道**刻意无 VALIDATE 节点**，闸4 是唯一确定性把关。

    锁法：monkeypatch `validate_contract_ownership`，捕获实参——断"这个值到达了闸"，
    而非断源码文本（纪律 6）。
    """
    import swarm.brain.plan_inject as pi

    seen: dict = {}

    def _spy(plan, sc, *, project_path=None, layout_punted=None, **kw):
        seen["layout_punted"] = layout_punted
        from swarm.brain.plan_validator import PlanValidationResult
        return PlanValidationResult(valid=True)

    _orig = pi.__dict__.get("validate_contract_ownership")
    # 该名字在函数体内 lazy import ⇒ 需 patch 定义模块
    import swarm.brain.plan_validator as pv
    _orig_pv = pv.validate_contract_ownership
    try:
        pv.validate_contract_ownership = _spy  # type: ignore[assignment]
        _probe_inject_c1_call(seen)
    finally:
        pv.validate_contract_ownership = _orig_pv  # type: ignore[assignment]
        if _orig is not None:
            pi.validate_contract_ownership = _orig  # type: ignore[attr-defined]

    assert "layout_punted" in seen, "C1 未被调用（夹具没走到闸4）"
    assert seen["layout_punted"] is not None, \
        "注入通道未传 layout_punted ⇒ 布局闸硬打回在本通道结构性失效（A1-M1）"
    assert "GhostSym→ghost/NoSrc.java" in (seen["layout_punted"] or []), \
        f"传的不是 finish 算出的那份 punt 账: {seen['layout_punted']}"


def _probe_inject_c1_call(seen: dict) -> None:
    """驱动**真** `prepare_injected_state` 走到闸4，把 finish 的 punt 账固定成已知值。

    ★必须驱动真入口★（批 D 的 d9 教训）：若只调 `validate_contract_ownership` 自己，
    测的是闸而不是"注入通道把账传给了闸"，而 finding 说的正是后者。
    """
    import swarm.brain.plan_finisher as pf
    import swarm.brain.plan_inject as pi

    _orig_pf = pf.finish_plan_deterministic

    def _fake_finish(plan, file_plan, **kw):
        # 只固定 punt 账，其余保持 finisher 的真实行为无关紧要（闸4 只读这一个键）
        return {"contract_symbols_layout_punted": ["GhostSym→ghost/NoSrc.java"],
                "scaffolds": {}}

    _cassette = {
        "schema": pi.CASSETTE_SCHEMA,
        "base_commit": None,
        "plan": TaskPlan(
            subtasks=[_st("st-a", create=["m/src/main/java/com/x/A.java"])]).model_dump(),
        "shared_contract": {"interfaces": [{"symbol": "GhostSym"}]},
        "file_plan": [],
        "task_description": "probe",
    }
    try:
        pf.finish_plan_deterministic = _fake_finish  # type: ignore[assignment]
        try:
            pi.prepare_injected_state(
                _cassette, live_base_commit=None, project_path=None)
        except Exception:  # noqa: BLE001 — 只关心闸4 收到什么实参；后续步骤失败无妨
            pass
    finally:
        pf.finish_plan_deterministic = _orig_pf  # type: ignore[assignment]


def test_m1_surgery_channel_passes_layout_punted_too():
    """sibling：外科候选择优处口径同源（漏传只影响候选评分，但判据不同源本身是漂移源头）。

    ★同 e9/e11 教训（本条初版也是 getsource 假绿）★：我在那两行上方的注释里就写了
    `layout_punted`，摘掉真实参仍绿。现改为 spy 捕获**实参**——断"这个值到达了闸"。
    """
    from swarm.brain import plan_validator as pv
    from swarm.brain import symbol_surgery as ss

    seen: dict = {}

    def _spy(plan, sc, *, project_path=None, layout_punted=None, **kw):
        seen["layout_punted"] = layout_punted
        return pv.PlanValidationResult(valid=True)

    _orig_vco = pv.validate_contract_ownership
    _orig_struct = pv.validate_plan_structure
    _orig_attach = ss.surgical_symbol_attach
    _orig_inject = None
    import swarm.brain.contract_utils as cu
    _orig_inject = cu.inject_build_scaffold_subtasks
    try:
        pv.validate_contract_ownership = _spy  # type: ignore[assignment]
        pv.validate_plan_structure = lambda *a, **k: pv.PlanValidationResult(valid=True)  # type: ignore[assignment]
        ss.surgical_symbol_attach = lambda *a, **k: {  # type: ignore[assignment]
            "attached": [], "baseline_owned": [], "remainder": []}
        cu.inject_build_scaffold_subtasks = lambda *a, **k: {}  # type: ignore[assignment]
        prior = _cycle_plan()
        ss.maybe_symbol_repair(
            {"plan": prior, "shared_contract": {"interfaces": [{"symbol": "GhostSym"}]},
             "contract_symbols_layout_punted": ["GhostSym→ghost/NoSrc.java"],
             "tech_design_file_plan": [],
             # 入口守卫：必须是【符号类失败重试轮】才进本通道
             # （_SYMBOL_ISSUE_MARKERS = ("契约符号无 owner", "规则5")）
             "plan_validation_feedback": "- 契约符号无 owner 子任务承接: GhostSym",
             "plan_validation_issues": ["契约符号无 owner 子任务承接: GhostSym"]},
            project_path=None)
    finally:
        pv.validate_contract_ownership = _orig_vco  # type: ignore[assignment]
        pv.validate_plan_structure = _orig_struct  # type: ignore[assignment]
        ss.surgical_symbol_attach = _orig_attach  # type: ignore[assignment]
        cu.inject_build_scaffold_subtasks = _orig_inject  # type: ignore[assignment]

    assert "layout_punted" in seen, "外科通道未走到 C1 复核（夹具没走通）"
    assert seen["layout_punted"] == ["GhostSym→ghost/NoSrc.java"], \
        f"外科通道未传 punt 账 ⇒ 候选看起来比实际更好，到 live VALIDATE 才被打回: {seen}"


# ───────────────── A1-L1：文档/签名与返回值同源 ─────────────────

def test_l1_route_double_claims_signature_matches_reality():
    """A1-L1：返回值是二元组，签名与 docstring 必须如实反映。

    锁在**行为**上：解包成两个 dict 必须成功，且 docstring 必须提到 weak 半的语义。
    """
    import inspect

    from swarm.brain.plan_validator import _cross_cluster_route_double_claims as _f

    plan = TaskPlan(subtasks=[_st("st-1", create=["m/src/main/java/com/x/AController.java"])])
    res = _f(plan)
    assert isinstance(res, tuple) and len(res) == 2, f"返回值形状变了: {type(res)}"
    strong, weak = res
    assert isinstance(strong, dict) and isinstance(weak, dict)

    _doc = inspect.getdoc(_f) or ""
    assert "weak" in _doc, "docstring 未说明 weak 通道（文档说的不是运行的东西）"
    _ann = inspect.signature(_f).return_annotation
    assert "tuple" in str(_ann), f"签名仍标 dict，与真实返回值不符: {_ann}"
