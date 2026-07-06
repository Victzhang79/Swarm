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
                "deliver_auto_reject_reason", "l2_details"):
        assert key in ann, f"实际通道 {key} 必须在 BrainState 声明（未声明=LangGraph 静默丢弃）"


def _registered_node_fn_names() -> set[str]:
    tree = ast.parse((_BRAIN / "graph.py").read_text())
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_node" and len(n.args) >= 2:
            name = getattr(n.args[1], "id", None) or getattr(n.args[1], "attr", None)
            if name:
                names.add(name)
    return names


def test_node_written_state_keys_are_declared():
    declared = set(BrainState.__annotations__)
    node_fns = _registered_node_fn_names()
    assert node_fns, "graph.py 应能解析出注册节点"
    offenders: dict[str, set[str]] = {}
    for f in list((_BRAIN / "nodes").glob("*.py")) + [_BRAIN / "runner.py"]:
        try:
            tree = ast.parse(f.read_text())
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name in node_fns):
                continue
            keys: set[str] = set()
            for x in ast.walk(fn):
                if isinstance(x, ast.Return) and isinstance(x.value, ast.Dict):
                    keys.update(k.value for k in x.value.keys
                                if isinstance(k, ast.Constant) and isinstance(k.value, str))
                if isinstance(x, ast.Assign):
                    for tgt in x.targets:
                        if (isinstance(tgt, ast.Subscript) and isinstance(tgt.value, ast.Name)
                                and tgt.value.id in ("out", "_patch", "patch", "update", "result")
                                and isinstance(tgt.slice, ast.Constant)
                                and isinstance(tgt.slice.value, str)):
                            keys.add(tgt.slice.value)
            missing = keys - declared - _ALLOWLIST
            if missing:
                offenders[f"{f.name}:{fn.name}"] = missing
    assert not offenders, (
        f"节点写了未声明的 state 键（LangGraph 会静默丢弃=死功能）：{offenders}；"
        "在 brain/state.py 补声明，或若非 state patch 键则加入本测 _ALLOWLIST 并注明"
    )
