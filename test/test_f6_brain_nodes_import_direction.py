#!/usr/bin/env python3
"""F6（round28，结构 P2）：brain ↔ nodes 循环依赖靠【惰性导入】维系，无护栏易被无意打回硬循环。

现状：brain/nodes/verify.py、dispatch.py 里 `from swarm.brain import nodes` 全部【函数体内】惰性
导入（破 import 期循环）。一旦有人把它挪到模块顶层，就会重新形成硬 import cycle（import 期 partial
module / ImportError），且往往在特定加载顺序下才炸、难复现。本测试锁死方向不变量：

    brain/nodes/**.py 中对【聚合层 swarm.brain / swarm.brain.nodes】的 back-import 只允许出现在
    函数/方法体内（惰性），绝不允许出现在模块顶层。

新增子模块若确需引用聚合层，也必须惰性导入——本护栏会拦下顶层写法。
"""
from __future__ import annotations

import ast
import pathlib

_NODES_DIR = pathlib.Path(__file__).resolve().parent.parent / "brain" / "nodes"


def _is_backedge_import(node: ast.AST) -> bool:
    """是否是对【聚合层 nodes 包对象】的 back-import——即引用 swarm.brain.nodes 的 __init__。

    ★精确锁定文档化的破环点★：`from swarm.brain import nodes`（拿到 nodes 包对象）与
    `import swarm.brain.nodes`。这类会触发 __init__ 加载→回过头 import 本子模块→硬循环，故必须惰性。
    【不】算 back-edge 的（合法顶层）：`from swarm.brain.nodes.shared import x` 等【叶子子模块】导入
    （intra-package，不经 __init__ 聚合，无环），以及 __init__.py 自身对子模块的聚合导入。
    """
    if isinstance(node, ast.ImportFrom):
        mod = node.module or ""
        if mod == "swarm.brain" and any(a.name == "nodes" for a in node.names):
            return True
        if mod == "swarm.brain.nodes":  # 从聚合 __init__ 导入（注意：不含 .shared 等叶子子模块）
            return True
    if isinstance(node, ast.Import):
        return any(a.name == "swarm.brain.nodes" for a in node.names)
    return False


def test_no_module_level_backedge_into_aggregator():
    offenders: list[str] = []
    for f in _NODES_DIR.glob("*.py"):
        if f.name == "__init__.py":
            continue  # 聚合层自身：对子模块的顶层聚合导入是其职责，非 back-edge
        tree = ast.parse(f.read_text(), filename=str(f))
        # 只看模块【顶层】语句：tree.body 仅含模块级语句，函数/方法体内的惰性 back-import 不在此列
        # （它们嵌在 FunctionDef 节点内），故只需判定顶层语句本身是否为 back-import。
        for stmt in tree.body:
            if _is_backedge_import(stmt):
                offenders.append(f"{f.name}:{getattr(stmt, 'lineno', '?')}")
    assert not offenders, (
        "brain/nodes/*.py 顶层出现对聚合层的 back-import（会形成硬 import cycle）："
        f"{offenders}；必须改为函数体内惰性导入"
    )


def test_lazy_backedges_still_present_are_function_local():
    """反向确认：现存的 `from swarm.brain import nodes` 都在函数体内（惰性），不误报也不漏防。"""
    found_lazy = 0
    for f in _NODES_DIR.glob("*.py"):
        tree = ast.parse(f.read_text(), filename=str(f))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(fn):
                    if sub is not fn and _is_backedge_import(sub):
                        found_lazy += 1
    assert found_lazy >= 1, "预期至少存在若干函数体内惰性 back-import（verify/dispatch）"
    print(f"  ✅ 惰性 back-import {found_lazy} 处，均在函数体内")


if __name__ == "__main__":
    test_no_module_level_backedge_into_aggregator()
    test_lazy_backedges_still_present_are_function_local()
    print("F6 import 方向护栏通过。")
