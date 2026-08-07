"""P1-DEBT-02 回归（SWARM_CTO_GUIDE §4）：_plan_ultra_batched 读错键 tech_design_result。

原 bug：读 state['tech_design_result']（全项目无人写）→ td 恒空 →
批间模块依赖排序(module_deps)失效 + data_model/契约注入空。
修复：键名改 tech_design；契约改取 shared_contract_draft。

★本文件已从「源码子串守卫」改为行为级驱动★（29 号文 T-A8）：
原两条测试断的是源码里出现 `state.get("tech_design")` / `shared_contract_draft` /
`td.get("modules")` 三个字面量。突变判据证明它们零区分力——保留 `td = state.get("tech_design")`
一行但下游改用 `{}`（如 `module_deps = {}`），或把 `td.get("modules")` 挪进死代码，
三条子串仍在 ⇒ 绿，而 td 对排序与注入恒空＝原 bug 逐字复发。该文件旧 docstring 还自述
「本测用源码静态断言守护」，而 `test_methodology_hard_checks.py` 明确把这种写法列为
**已实证的假绿**。

现在的判据是**可观测后果**：
  ① 批次顺序按 tech_design.modules.depends_on 拓扑排（夹具刻意让拓扑序与回退序相反：
     拓扑=mod_c→mod_b→mod_a，回退（字母/分层）=mod_a→mod_b→mod_c）；
  ② data_model 与 shared_contract_draft 的内容真的进了 prompt。
"""
from __future__ import annotations

import json

import pytest

# 夹具：三模块链式依赖 mod_a→mod_b→mod_c，故拓扑序为 c, b, a（与字母序恰好相反）
_FILE_PLAN = [
    {"path": "mod_a/A.java", "module": "mod_a", "action": "create"},
    {"path": "mod_b/B.java", "module": "mod_b", "action": "create"},
    {"path": "mod_c/C.java", "module": "mod_c", "action": "create"},
]
_TECH_DESIGN = {
    "modules": [
        {"name": "mod_a", "depends_on": ["mod_b"]},
        {"name": "mod_b", "depends_on": ["mod_c"]},
        {"name": "mod_c", "depends_on": []},
    ],
    "data_model": "DM_MARKER_数据模型正文",
}
_CONTRACT_DRAFT = {"interfaces": [{"name": "IContractMarker", "module": "mod_c",
                                   "signature": "ping():void"}]}
_MODS = ("mod_a", "mod_b", "mod_c")


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _CapturingLLM:
    """记录每批 prompt，并回一个属于该批模块的合法子任务。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def ainvoke(self, msgs):
        txt = "\n".join(m["content"] if isinstance(m, dict) else str(m) for m in msgs)
        self.prompts.append(txt)
        mod = next((m for m in _MODS if f"模块 '{m}'" in txt), "unknown")
        return _Resp(json.dumps({"subtasks": [{
            "id": f"st-{mod}",
            "description": f"实现 {mod}",
            "difficulty": "medium",
            "modality": "text",
            "scope": {"create_files": [f"{mod}/X.java"], "writable": [], "readable": []},
            "depends_on": [],
            "acceptance": ["mvn -q compile"],
        }]}, ensure_ascii=False))

    def batch_order(self) -> list[str]:
        """还原批次顺序：按 prompt 里的 **批次编号** `第 N/M 批`，而非 prompt 到达顺序。

        ★不能用 `self.prompts` 的 append 顺序★（双复核 L-3）：`_plan_ultra_batched` 用
        `gather_cancel_on_error` **并发**派发，`_plan_sem = Semaphore(4)` 容得下全部 3 批
        ⇒ append 顺序由事件循环调度决定，不是被测语义（实测现象稳定，但机制上无保证）。
        生产真正要保的是「批次编号 → 模块」这个映射（`merge_subtask_batches` 靠它连边），
        而 `PLAN_BATCH_USER` 模板里就带着 `第 {batch_idx}/{total_batches} 批`。
        """
        import re

        by_idx: dict[int, str] = {}
        for p in self.prompts:
            m_idx = re.search(r"第\s*(\d+)\s*/\s*\d+\s*批", p)
            mod = next((m for m in _MODS if f"模块 '{m}'" in p), None)
            if m_idx and mod:
                by_idx[int(m_idx.group(1))] = mod
        assert by_idx, "一条 prompt 都没解析出批次编号+模块名 ⇒ 夹具或模板变了，下面的断言会 vacuous"
        return [by_idx[k] for k in sorted(by_idx)]


async def _run(state_extra: dict) -> _CapturingLLM:
    import swarm.brain.nodes as nodes_mod

    llm = _CapturingLLM()
    state = {
        "complexity": "ultra",
        "task_description": "建预警平台",
        "shared_contract_draft": _CONTRACT_DRAFT,
        **state_extra,
    }
    await nodes_mod._plan_ultra_batched(llm, state, "建预警平台", {}, "", _FILE_PLAN)
    return llm


@pytest.mark.asyncio
async def test_batch_order_follows_tech_design_module_deps():
    """批间顺序必须按 tech_design.modules.depends_on 拓扑排（td 恒空则退化成字母/分层序）。"""
    llm = await _run({"tech_design": _TECH_DESIGN})
    assert llm.batch_order() == ["mod_c", "mod_b", "mod_a"], (
        f"批次顺序 {llm.batch_order()} 不是 tech_design 拓扑序 ⇒ module_deps 没生效"
        "（td 读错键/下游改用空字典都会落到这里）"
    )


@pytest.mark.asyncio
async def test_batch_order_degrades_when_tech_design_absent():
    """诚实边界锁：td 缺席时确实退化成另一种顺序 —— 证明上一条的断言有区分力。

    没有这一条，「拓扑序 == [c,b,a]」可能只是回退启发式恰好也给出同样顺序
    （夹具形状决定命题唯一性，血规 10②）。
    """
    llm = await _run({"tech_design": {}})
    fallback = llm.batch_order()
    assert fallback == ["mod_a", "mod_b", "mod_c"], f"回退序实得 {fallback}"
    assert fallback != ["mod_c", "mod_b", "mod_a"], \
        "回退序与拓扑序相同 ⇒ 本夹具无区分力，必须换成两序不同的 file_plan"


@pytest.mark.asyncio
async def test_data_model_and_contract_reach_prompt():
    """data_model（来自 td）与 shared_contract_draft（来自 state）都必须进 prompt。"""
    llm = await _run({"tech_design": _TECH_DESIGN})
    joined = "\n".join(llm.prompts)
    assert "DM_MARKER_数据模型正文" in joined, \
        "data_model 没进 prompt ⇒ td 恒空（P1-DEBT-02 原病）"
    assert "IContractMarker" in joined, \
        "shared_contract_draft 没进 prompt ⇒ 契约取错来源（td.get('shared_contract') 恒空）"


@pytest.mark.asyncio
async def test_data_model_absent_when_tech_design_absent():
    """诚实边界锁：td 缺席时 data_model 确实不进 prompt（证明上一条有区分力）。"""
    llm = await _run({"tech_design": {}})
    # ★纯负向断言必须自带非空前提★（hunter F7 同族）：`prompts` 为空时
    # `"X" not in ""` 恒真 ⇒ vacuous 通过。原先这个前提由兄弟条
    # test_batch_order_degrades_when_tech_design_absent 背书（同一 _run 路径）＝区分力外包，
    # 形状与 W-25 F-3 一致。加这一行让它自洽。
    assert llm.prompts, "一次 LLM 调用都没发生 ⇒ 下面的负向断言 vacuous 通过"
    assert "DM_MARKER_数据模型正文" not in "\n".join(llm.prompts)
