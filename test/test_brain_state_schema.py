"""CODEWALK 根因A：TypedDict schema 落后实际通道集——且【未声明=静默丢弃】已实证坐实。

实证（本仓 langgraph 版本，toy StateGraph 复现）：节点返回 dict 里未声明的键、以及
initial_state 里未声明的键，都被 LangGraph 静默丢弃（无 channel）。审计原以为
"靠宽容存活"——实际是 base_commit（runner 注入、14+ 读者恒拿 None 走回退）/
plan_generation_failed（after_validate 闸门死代码）/ deliver_auto_reject_reason
（runner 判定永不触发）三条链路整体失活。补声明=激活这些设计链路。

本测两半：①锁定 4 键已声明；②AST 一致性扫描——graph.py 注册的节点函数体内
`out[...]=`/`return {字面量}` 写的 state 键必须全部在 BrainState 声明，
防未来新增节点再写出"被静默丢弃的死功能"。
"""
from __future__ import annotations

import ast
import pathlib

from swarm.brain.state import BrainState

_BRAIN = pathlib.Path(__file__).resolve().parent.parent / "brain"

# LLM 结果 dict 的键（result["..."]= / mock 返回体），非 state patch——已人工核实的误报
_ALLOWLIST = {"reasoning", "subtasks"}


def test_previously_undeclared_channels_now_in_schema():
    ann = BrainState.__annotations__
    for key in ("base_commit", "plan_generation_failed",
                "deliver_auto_reject_reason", "l2_details",
                # F7(round28)：tech_design 节点在 brain/planning_nodes.py（旧 glob 只扫 brain/nodes/
                # 漏了它），写此闸门标记却未声明 → 被静默丢 → gates.py:66 死代码。
                "tech_design_generation_failed"):
        assert key in ann, f"实际通道 {key} 必须在 BrainState 声明（未声明=LangGraph 静默丢弃）"


def test_runtime_smoke_keys_declared():
    """S1-4（task#18）：verify_runtime 三态/详情/转交 sid + migration 验证键必须声明。

    不声明 → verify_runtime 写的结论被 LangGraph 静默丢弃：after_verify_runtime 路由
    永远读 None（失败也放行 L3=假绿）、failed 的归因详情蒸发（task#20 回灌成死功能）。
    """
    ann = BrainState.__annotations__
    for key in ("runtime_smoke_passed", "runtime_smoke_skipped", "runtime_smoke_message",
                "runtime_smoke_details", "runtime_smoke_sandbox_id",
                "migration_verify_passed", "migration_verify_details"):
        assert key in ann, f"S1-4 键 {key} 必须在 BrainState 声明（未声明=LangGraph 静默丢弃）"


def test_s2_acceptance_keys_declared():
    """S2（task S2-2）：需求条目/验收断言四键必须声明（声明先行，migration 键先例）。

    requirement_items 由 extract_requirements 节点本批写入；acceptance_assertions/
    acceptance_passed/acceptance_details 由 task#25/26 写入——先声明，否则届时写入
    会被 LangGraph 静默丢弃成死功能（覆盖校验/auto-accept 闸门读到恒 None）。
    """
    ann = BrainState.__annotations__
    for key in ("requirement_items", "acceptance_assertions",
                "acceptance_passed", "acceptance_details"):
        assert key in ann, f"S2 键 {key} 必须在 BrainState 声明（未声明=LangGraph 静默丢弃）"


def _registered_node_fn_names() -> set[str]:
    tree = ast.parse((_BRAIN / "graph.py").read_text())
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_node" and len(n.args) >= 2:
            name = getattr(n.args[1], "id", None) or getattr(n.args[1], "attr", None)
            if name:
                names.add(name)
    return names


def _node_written_keys(fn: ast.AST) -> set[str]:
    """收集节点函数【自身作用域】写入的 state patch 键：`return {字面量}` 与
    `out/_patch/patch/update/result[...] =`。★不下钻嵌套 helper 函数体★——否则内层
    per-module 结果 dict（如 contract_design 内 `return {"idx","name","slice","error"}`）
    会被误当成外层节点的 state patch（F7 扩 glob 到 planning_nodes.py 时实测的假阳性）。
    """
    keys: set[str] = set()

    def _visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # 嵌套函数（inner helper / 闭包）自成作用域，其返回不是本节点的 state patch → 不下钻。
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Return) and isinstance(child.value, ast.Dict):
                keys.update(k.value for k in child.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str))
            if isinstance(child, ast.Assign):
                for tgt in child.targets:
                    if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
                            and tgt.value.id in ("out", "_patch", "patch", "update", "result")
                            and isinstance(tgt.slice, ast.Constant)
                            and isinstance(tgt.slice.value, str)):
                        keys.add(tgt.slice.value)
            _visit(child)

    _visit(fn)
    return keys


def test_node_written_state_keys_are_declared():
    declared = set(BrainState.__annotations__)
    node_fns = _registered_node_fn_names()
    assert node_fns, "graph.py 应能解析出注册节点"
    offenders: dict[str, set[str]] = {}
    # F7(round28)：旧 glob 只扫 brain/nodes/*.py + runner.py，漏了直属 brain/ 的
    # planning_nodes.py——tech_design/plan/contract_design 等注册节点就在那里，其写出的
    # 未声明键（tech_design_generation_failed）被静默丢成死代码却无人拦。扩到 brain/*.py。
    scanned = list(_BRAIN.glob("*.py")) + list((_BRAIN / "nodes").glob("*.py"))
    for f in scanned:
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in node_fns):
                continue
            missing = _node_written_keys(fn) - declared - _ALLOWLIST
            if missing:
                offenders[f"{f.name}:{fn.name}"] = missing
    assert not offenders, (
        f"节点写了未声明的 state 键（LangGraph 会静默丢弃=死功能）：{offenders}；"
        "在 brain/state.py 补声明，或若非 state patch 键则加入本测 _ALLOWLIST 并注明"
    )
