#!/usr/bin/env python3
"""全仓突变 harness 的【落点静态审计】——不运行任何 harness，只做 AST 解析 + 字符串定位。

## 为什么需要它

突变 harness 的落点是**源码字面量**。被测代码重构/改写后落点会漂移，而 harness 只会打一行
"落点未命中"就跳过那一条 —— 于是那条锁**静默零覆盖**，harness 整体仍报"全部通过"（若它
没有把落点失效单独计数的话，甚至连那行提示都不显眼）。

已实测两个实例：
  · `xm_mutation_check.py` 的 X-M10a：`DRIVERS` 表从两项单行长到六项多行、且迁了模块
    ⇒ 该锁自扩表起一直零覆盖（#29-2 顺手修掉）。
  · 29 号文登记的「24 条死突变锁」——但那份枚举是人工列的，**同一份心智模型的缺口会
    重合**（本仓已登记的教训：为漏项造的兜底网不能用同一份枚举编）。故本脚本用**机器**
    重扫全仓，不依赖那 24 条清单。

## 判据

对每个 `scripts/*_mutation_check.py`，静态取出 `MUTATIONS` 列表里每条的 (path, old)：
  · old 在目标文件里出现 **0 次** → ★死锁★（该突变永不施加）
  · 出现 **≥2 次** → ★非唯一★（`replace(old, new, 1)` 只改第一处，突变语义不等价）
  · 出现 **1 次** → 健康

## 安全性

**只读**：`ast.literal_eval` 取字面量，绝不 import/exec harness 模块，绝不写任何文件。
故它与全量测试、与别人的复核可以安全并发（与 harness 本体相反）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _resolve_path_expr(node: ast.AST, consts: dict[str, str]) -> str | None:
    """把 harness 里的路径表达式还原成相对仓根的路径。

    支持两种写法：① 模块级常量名（`L1`/`WM`/`DL`…，值形如 `ROOT / "worker" / "x.py"`）；
    ② 内联的 `ROOT / "worker" / "x.py"`。还原不出 → None（调用方计入"无法静态判定"，
    绝不猜——猜错会把健康锁报成死锁，比漏报更坏）。
    """
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _resolve_path_expr(node.left, consts)
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            if left == "<ROOT>":
                return right.value
            if left:
                return f"{left}/{right.value}"
        return None
    if isinstance(node, ast.Name) and node.id == "ROOT":
        return "<ROOT>"
    return None


def _collect_path_consts(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {"ROOT": "<ROOT>"}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        tgt = stmt.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        resolved = _resolve_path_expr(stmt.value, consts)
        if resolved and resolved != "<ROOT>":
            consts[tgt.id] = resolved
    return consts


def _iter_mutations(tree: ast.Module, consts: dict[str, str]):
    """yield (index, path_rel_or_None, old_str_or_None)。"""
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        tgt = stmt.targets[0]
        if not (isinstance(tgt, ast.Name) and tgt.id == "MUTATIONS"):
            continue
        if not isinstance(stmt.value, (ast.List, ast.Tuple)):
            continue
        for i, elt in enumerate(stmt.value.elts, 1):
            if not isinstance(elt, (ast.Tuple, ast.List)) or len(elt.elts) < 3:
                yield i, None, None
                continue
            path_rel = _resolve_path_expr(elt.elts[1], consts)
            try:
                old = ast.literal_eval(elt.elts[2])
            except Exception:  # noqa: BLE001 — 含变量拼接等非字面量写法
                old = None
            yield i, path_rel, (old if isinstance(old, str) else None)


class AuditResult:
    """审计结果的结构化载体 —— **CLI 与测试共用同一份计算**。

    T-1 治法② 的关键：审计不能只有一个"人来看"的 CLI 出口。原 24 条死锁之所以能活很久，
    正是因为 harness 自己会打印"落点未命中"并 `return 1`，但**没有任何自动化消费那个返回
    值**（`grep -rn mutation_check .github/` 与 `test/` 皆为 0）。故这里把计算与呈现分离，
    让 `test/test_harness_landing_locks.py` 消费同一个函数——审计有了机读消费者，第 24 条
    漂移才不会同样静默。
    """

    def __init__(self) -> None:
        self.dead: list[tuple[str, int, str, str]] = []
        self.nonuniq: list[tuple[str, int, str, str, int]] = []
        self.undecidable: list[tuple[str, int, str]] = []
        self.healthy = 0
        self.total = 0
        self.harness_count = 0

    @property
    def ok(self) -> bool:
        return not self.dead and not self.nonuniq


def audit() -> AuditResult:
    """静态审计全仓 harness 落点。纯只读（AST + 字符串计数），可与全量测试并发。"""
    res = AuditResult()
    harnesses = sorted(ROOT.glob("scripts/*mutation_check.py"))
    res.harness_count = len(harnesses)
    file_cache: dict[str, str | None] = {}

    def _read(rel: str) -> str | None:
        if rel not in file_cache:
            p = ROOT / rel
            try:
                file_cache[rel] = p.read_text(encoding="utf-8")
            except OSError:
                file_cache[rel] = None
        return file_cache[rel]

    for h in harnesses:
        try:
            tree = ast.parse(h.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            res.undecidable.append((h.name, 0, f"harness 自身语法错误: {exc}"))
            continue
        consts = _collect_path_consts(tree)
        for idx, path_rel, old in _iter_mutations(tree, consts):
            res.total += 1
            if path_rel is None or old is None:
                res.undecidable.append((h.name, idx, "路径或 old 非字面量，静态判不了"))
                continue
            src = _read(path_rel)
            if src is None:
                res.dead.append((h.name, idx, path_rel, "目标文件不存在"))
                continue
            n = src.count(old)
            snippet = old.strip().splitlines()[0][:70] if old.strip() else "(空)"
            if n == 0:
                res.dead.append((h.name, idx, path_rel, snippet))
            elif n > 1:
                res.nonuniq.append((h.name, idx, path_rel, snippet, n))
            else:
                res.healthy += 1
    return res


def main() -> int:
    res = audit()
    if not res.harness_count:
        print("未找到任何 scripts/*mutation_check.py")
        return 1
    harnesses = [f"{res.harness_count} 个"]
    dead, nonuniq, undecidable = res.dead, res.nonuniq, res.undecidable
    healthy, total = res.healthy, res.total

    print("═" * 78)
    print(f"全仓突变 harness 落点静态审计：{harnesses[0]} harness / {total} 条突变")
    print("═" * 78)
    print(f"  健康（落点唯一）      ：{healthy}")
    print(f"  ★死锁（落点 0 次）    ：{len(dead)}")
    print(f"  ★非唯一（落点 ≥2 次） ：{len(nonuniq)}")
    print(f"  静态判不了            ：{len(undecidable)}")

    if dead:
        print("\n" + "─" * 78)
        print("★死锁明细（这些突变永不施加 ⇒ 该条锁零覆盖）")
        print("─" * 78)
        for name, idx, path_rel, snippet in dead:
            print(f"  {name} #{idx}  → {path_rel}")
            print(f"      落点: {snippet}")
    if nonuniq:
        print("\n" + "─" * 78)
        print("★非唯一明细（replace(...,1) 只改第一处 ⇒ 突变语义不等价）")
        print("─" * 78)
        for name, idx, path_rel, snippet, n in nonuniq:
            print(f"  {name} #{idx}  → {path_rel}（{n} 次）")
            print(f"      落点: {snippet}")
    if undecidable:
        print("\n" + "─" * 78)
        print("静态判不了（需人工看；非字面量落点本身也是可维护性债）")
        print("─" * 78)
        for name, idx, why in undecidable:
            print(f"  {name} #{idx}：{why}")

    print()
    return 1 if (dead or nonuniq) else 0


if __name__ == "__main__":
    sys.exit(main())
