"""26 号文 G 路·上游自激批：C-8（答案被扔）+ C-7（主动制造缺陷）。

这两条合起来就是 round67m2 FAILED@PLAN 的完整死因链上游段——闸门从头到尾判得全对，
每一条 ③f REJECT 都指向真实的启动崩溃，问题是**被打回的东西上游结构上必然会再产生一遍**。
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from swarm.brain.nodes import (
    _already_exists_prompt_block,
    _design_files_for_unplanned,
    _ensure_file_plan_covers_requirements,
)


# ══════════════════════════════════════════════
# C-8：already_exists 全仓零消费者
# ══════════════════════════════════════════════

_AE = {"verdict": "already_exists",
       "claim": "新增代码生成器模块",
       "detail": "ruoyi-generator 已含 GenTable/VelocityUtils",
       "suggestion": "直接复用既有模块，不新建"}


def test_already_exists_reaches_planner():
    """★大模型在 T-2h 就答对了本轮死因，系统把答案扔了（26 号文 C-8）★
    `already_exists` 此前只出现在 3 处 prompt 文本 + 1 处 state 注释，全仓零读取点——
    两个消费者（after_tech_design / clarify）都写死 `verdict == "false"`。
    tech_design STAGE1 原话："代码生成器直接复用既有 ruoyi-generator…不新建"。"""
    blk = _already_exists_prompt_block({"tech_design_fact_issues": [_AE]})
    assert "ruoyi-generator" in blk and "基线已有" in blk


def test_already_exists_still_requires_verifiable_reason():
    """★不能直接标 baseline_covered（那是 fail-open）★
    already_exists 是 LLM 判断不是确定性证据；直接据此跳过实现＝真该做的活被静默跳过。
    与 A7 候选严格同构：作为【申报候选】呈现，仍要求可核实路径、仍过接地校验。"""
    blk = _already_exists_prompt_block({"tech_design_fact_issues": [_AE]})
    assert "可核实" in blk and "baseline_covered" in blk
    assert "部分满足" in blk, "只部分满足时必须补齐差额，绝不整条跳过"


def test_already_exists_block_empty_without_hits():
    """无 already_exists（绝大多数轮）→ 一字不加，零噪声零回归。"""
    assert _already_exists_prompt_block({"tech_design_fact_issues": [{"verdict": "false"}]}) == ""
    assert _already_exists_prompt_block({}) == ""


@pytest.mark.asyncio
async def test_already_exists_survives_a7_channel_failure(monkeypatch):
    """★两条线索来源独立，A7 挂了不能连累 already_exists★
    A7 走知识库索引（可能未建/异常），already_exists 走 tech_design 产出——
    把它塞进 A7 的 try 里，等于让一条通道的降级静默吃掉另一条。"""
    from swarm.brain import nodes
    from swarm.knowledge import service as ksvc

    async def _boom(*a, **k):
        raise RuntimeError("索引未建")

    monkeypatch.setattr(ksvc, "fetch_structure_inventory", _boom)
    blk = await nodes._baseline_candidates_block_for({
        "requirement_items": [{"id": "req-1", "text": "x"}],
        "project_id": "p1",
        "tech_design_fact_issues": [_AE],
    })
    assert "ruoyi-generator" in blk


def test_already_exists_wired_at_the_single_a7_chokepoint():
    """★接在 A7 块函数里是刻意的（防"新原语只接主调用点"）★
    A7 块有两个注入点（分批 / 单发）；另起一个块必然漏接其中一个——本轮深扫的元结论
    之一正是"新原语造对了只接主调用点"，已列 5 个新实例。"""
    from swarm.brain import nodes
    src = inspect.getsource(nodes._baseline_candidates_block_for)
    assert "_already_exists_prompt_block" in src


# ══════════════════════════════════════════════
# C-7：补排闸自激环
# ══════════════════════════════════════════════

class _FakeLLM:
    """每次被调用都造一套【全新命名】的平行设计——正是 round67m2 实测到的形态
    （三轮 69/48/64 个文件，Alarm→Alert 都换了名）。"""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        n = self.calls
        entries = ",".join(
            f'{{"path":"m/src/Gen{n}_{i}.java","module":"m","responsibility":"r"}}'
            for i in range(5))

        class _R:
            content = '{"file_plan":[' + entries + ']}'
        return _R()


def _umbrella_state(attempted=None):
    """伞形需求：判别 token 退化到只剩 'prd' —— 没有哪个文件名会含 'prd'，
    故 planned_vocab 永远匹配不上 → 每轮恒判 unplanned。"""
    return {
        "requirement_items": [{"id": "req-27e9b283", "text": "按 PRD 完整交付"}],
        "coverage_design_attempted_reqs": list(attempted or []),
        "project_id": "p1",
    }


@pytest.mark.asyncio
async def test_umbrella_requirement_does_not_retrigger_design(monkeypatch):
    """★核心：结构上永不可满足的需求必须只试一次（26 号文 C-7）★
    round67m2 实证：req-27e9b283 每轮触发补排，LLM 每轮凭空造一套平行设计，
    file_plan 234→303→350 单调膨胀 → 大量 create 撞 base → ③f 准确 REJECT → 熔断。
    与 B1 修复记忆同族：**没有"试过了"的记忆就会无界重入**。"""
    from swarm.brain import nodes

    async def _vocab(_s):
        return "someexistingclass"
    monkeypatch.setattr(nodes, "_baseline_vocab_for", _vocab)
    fp = [{"path": "m/src/A.java", "module": "m", "action": "create"}]
    llm = _FakeLLM()

    # 轮1：试了一次（LLM 被调用），补出来的文件仍覆盖不住伞形需求 → 记账
    _, _, att1 = await _ensure_file_plan_covers_requirements(_umbrella_state(), llm, fp)
    assert llm.calls == 1
    assert "req-27e9b283" in att1, "试过仍未覆盖 → 必须记进'试过了'账"

    # 轮2：带着账进来 → 不再重试（LLM 调用次数不增）
    _, aug2, att2 = await _ensure_file_plan_covers_requirements(
        _umbrella_state(att1), llm, fp)
    assert llm.calls == 1, "同一条需求绝不能第二次触发补排（自激环的关键一环）"
    assert aug2 is False
    assert att2 == att1


@pytest.mark.asyncio
async def test_llm_failure_is_not_recorded_as_attempted(monkeypatch):
    """基建故障 ≠ "设计不出来"——LLM 挂了下一轮该重试（fail-open），绝不记进抑制账。"""
    from swarm.brain import nodes

    async def _vocab(_s):
        return "someexistingclass"
    monkeypatch.setattr(nodes, "_baseline_vocab_for", _vocab)

    class _Boom:
        async def ainvoke(self, m):
            raise RuntimeError("provider 503")

    _, _, att = await _ensure_file_plan_covers_requirements(
        _umbrella_state(), _Boom(),
        [{"path": "m/src/A.java", "module": "m", "action": "create"}])
    assert att == [], "LLM 失败不得记账，否则一次网络抖动永久关闭本闸"


@pytest.mark.asyncio
async def test_design_prompt_carries_existing_file_plan():
    """★补排 prompt 此前【不含既有 file_plan】——模型只能凭空造整套设计（C-7 另一半）★
    它不是在"补排漏掉的文件"，是在"每轮重新设计一遍这个系统"：三轮三套互不相同的命名。"""
    captured = {}

    class _Cap:
        async def ainvoke(self, messages):
            captured["user"] = messages[-1]["content"]

            class _R:
                content = '{"file_plan":[]}'
            return _R()

    await _design_files_for_unplanned(
        _Cap(), [{"id": "req-1", "text": "告警渠道"}], "Spring Boot", ["m"],
        existing_file_plan=[{"path": "m/src/AlarmService.java", "module": "m"}])
    u = captured["user"]
    assert "AlarmService.java" in u, "既有文件必须进 prompt"
    assert "绝不要重复设计" in u and "换个近义词重起一套" in u
    assert "file_plan\":[]" in u, "必须给出'都已覆盖就输出空'的出口"


@pytest.mark.asyncio
async def test_design_prompt_caps_existing_list():
    """既有 file_plan 可达数百条——展示要封顶并如实说明截了多少（绝不静默截断）。"""
    from swarm.brain.nodes import _COVERAGE_DESIGN_EXISTING_CAP
    captured = {}

    class _Cap:
        async def ainvoke(self, messages):
            captured["user"] = messages[-1]["content"]

            class _R:
                content = '{"file_plan":[]}'
            return _R()

    big = [{"path": f"m/src/F{i}.java", "module": "m"}
           for i in range(_COVERAGE_DESIGN_EXISTING_CAP + 30)]
    await _design_files_for_unplanned(_Cap(), [{"id": "r", "text": "t"}], "x", ["m"],
                                      existing_file_plan=big)
    assert "另有 30 个文件未列出" in captured["user"]


def test_attempted_ledger_cleared_symmetrically_with_b1():
    """★清空点必须与 B1 修复记忆严格对称★
    两者都是"重试窗口内的记忆"：validate 通过 / replan 新周期都该重新给机会。
    少一个清空点 = 账粘滞到下一个规划周期，把本该重试的需求永久关在门外。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for f in ("brain/nodes/__init__.py", "brain/nodes/failure.py"):
        src = (root / f).read_text()
        assert src.count('"plan_validation_issue_history": []') == \
            src.count('"coverage_design_attempted_reqs": []'), \
            f"{f}: 两个账的清空点数量不等——必然有一处漏了"


def test_attempted_ledger_declared_in_state_schema():
    """★LangGraph 未在 schema 声明的键会被静默丢弃★（本仓头号血泪，CLAUDE.md 明列）"""
    from swarm.brain.state import ACCOUNTING_KEY_LIFECYCLE, BrainState
    assert "coverage_design_attempted_reqs" in BrainState.__annotations__
    assert ACCOUNTING_KEY_LIFECYCLE["coverage_design_attempted_reqs"] == "monotonic"


def test_attempted_ledger_write_back_is_unconditional():
    """★条件 emit = 陈旧持久化（ACCOUNTING_KEY_LIFECYCLE 血泪）★
    LangGraph 对缺席键保留旧值；"本轮没跑补排就不发"看似等价，实则让异常路径上的账
    静默陈旧。本键必须无条件回写。"""
    from swarm.brain import nodes
    src = inspect.getsource(nodes.plan)
    assert '"coverage_design_attempted_reqs": _cov_attempted,' in src
    assert '**({"coverage_design_attempted_reqs"' not in src


if not hasattr(pytest, "mark") or not hasattr(pytest.mark, "asyncio"):  # pragma: no cover
    asyncio  # noqa: B018 — 保持 import 被使用（无 pytest-asyncio 时的兜底）


# ══════════════════════════════════════════════
# G-H9：登记称已治但代码只治了一半
# ══════════════════════════════════════════════

def test_owner_ledger_scans_every_contract_section():
    """★"登记册打了 ✅ 而代码只治了一半"——本轮的元问题之一（26 号文 G-H9）★
    同文件的 `_contract_owner_authority` 早已按 R67G 修成扫全 section，而
    `contract_owner_ledger_block` 仍只扫 `interfaces`：枚举/DTO 的 defined_in 放在
    `dtos`/`types` 时（round67g 铁证 `AlarmLevelEnum`），分批 prompt 的 owner 清单里
    **没有它们** → 各批继续在别包 create 同名枚举 → ③b REJECT。
    ★当时的测试全用 `{"interfaces": [...]}` 造数据，回归零覆盖★——所以这条用例
    **刻意把 defined_in 放在非 interfaces 段**。"""
    from swarm.brain.contract_utils import contract_owner_ledger_block
    out = contract_owner_ledger_block({
        "interfaces": [{"defined_in": "m/src/main/java/com/x/AlarmService.java"}],
        "dtos": [{"defined_in": "m/src/main/java/com/x/AlarmLevelEnum.java"}],
        "types": [{"defined_in": "m/src/main/java/com/x/AlarmDTO.java"}],
    })
    for name in ("AlarmService", "AlarmLevelEnum", "AlarmDTO"):
        assert name in out, f"{name} 未进 owner 台账（section 覆盖面漏了）"


def test_owner_ledger_and_authority_share_the_section_scan():
    """★口径同源★：台账与权威两侧对"契约里哪些段带 defined_in"必须同口径，
    否则一侧防住的另一侧照样放行——这正是 G-H9 的形态。"""
    from swarm.brain.contract_utils import (
        _contract_owner_authority,
        contract_owner_ledger_block,
    )
    ct = {"dtos": [{"defined_in": "m/src/main/java/com/x/OnlyInDtos.java"}]}
    owners, _amb = _contract_owner_authority(ct)
    # 权威侧的 base 键含扩展名（`onlyindtos.java`），台账侧按类名展示——两者键形态不同
    # 是刻意的（用途不同），此处只断言"同一段里的 defined_in 两侧都认得出"。
    assert any(k.startswith("onlyindtos") for k in owners), "权威侧本就该认（R67G 已修）"
    assert "OnlyInDtos" in contract_owner_ledger_block(ct), "台账侧必须跟上"


def test_methodology_hard_checks_are_written_down():
    """★元教训必须写进 checked-in 的纪律文档，否则下一轮重犯★
    CLAUDE.md 每次会话都会加载，是杠杆最高的落点（而复盘文档 gitignore 不入库）。"""
    from pathlib import Path
    md = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text()
    for anchor in ("接线覆盖 ≠ 机制存在",
                   "测试要证\"被接上了\"",
                   "复用单一事实源 ≠ 复用其消费契约",
                   "必须机读可辨"):
        assert anchor in md, f"CLAUDE.md 缺方法论硬检查项：{anchor}"
