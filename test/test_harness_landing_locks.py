"""#29-3 T-1 治法②：突变 harness 的【落点健康】必须有机读消费者。

## 这个文件存在的理由（根因，不是补丁）

29 号文 T-1 实测：23 个 harness 里 **24 条落点已漂移**（`old` 在当前源码找不到）⇒ 那些锁
静默零覆盖。放大器是**没有任何自动化消费 harness 的返回值**：
  · `grep -rn 'mutation_check' .github/` = 0 处
  · `grep -rn 'mutation_check' test/`    = 0 处
harness 自己会打印「落点未命中（代码已漂移）」并 `return 1`（设计正确），但没人看。
⇒ 逐条修完那 24 条并不解决问题：**第 25 条漂移会同样静默**。故治本是给审计装消费者。

## 为什么用静态审计而不是跑 harness

harness 会**改磁盘源码**（写入→跑测试→还原）。放进测试套件会：与全量并发时污染工作树、
被 SIGKILL 跳过 `finally` 留下污染、与其它测试抢同一份源文件。本仓已登记多次血泪。
`scripts/harness_landing_audit.py` 是**纯只读**的（AST 取字面量 + 字符串计数），零副作用，
可与全量安全并发 —— 它验的正是"落点还在不在"这一维，恰好是漂移的唯一判据。

## 诚实边界

静态审计**只能**判「落点是否唯一存在」，**判不了**「突变后测试是否真会红」（那必须真跑
harness）。所以它不是 harness 的替代品，而是**漂移的早期告警**：落点没了 ⇒ 那条锁必然
零覆盖（充分条件）；落点在 ⇒ 只说明还能施加，不保证有区分力。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_AUDIT_PY = Path(__file__).resolve().parent.parent / "scripts" / "harness_landing_audit.py"


def _load_audit():
    """按路径加载审计脚本（scripts/ 不是包，不能 import）。"""
    spec = importlib.util.spec_from_file_location("harness_landing_audit", _AUDIT_PY)
    assert spec and spec.loader, f"无法加载 {_AUDIT_PY}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def result():
    return _load_audit().audit()


def test_audit_script_exists():
    """前提锁：脚本被删/改名 ⇒ 本文件其余测试全部空转，必须先钉住它在。"""
    assert _AUDIT_PY.is_file(), f"审计脚本不存在: {_AUDIT_PY}"


def test_audit_actually_examined_harnesses(result):
    """★前提锁（区分力的根）★：审计必须真的扫到了 harness 与突变条目。

    若解析器坏掉/glob 写错 ⇒ `total=0` ⇒ 下面「零死锁」恒真 = 假绿。本仓登记过同型教训
    （「实际检查了 N 条」这个计数必须先断言）。
    """
    assert result.harness_count >= 20, (
        f"只扫到 {result.harness_count} 个 harness，远少于本仓已有数量 ⇒ glob 或路径解析坏了")
    assert result.total >= 400, (
        f"只解析出 {result.total} 条突变，远少于本仓已有数量 ⇒ MUTATIONS 解析坏了")
    assert result.healthy > 0, "健康落点为 0 ⇒ 审计逻辑坏了（不可能全死）"


def test_no_dead_landing_points(result):
    """★核心断言★：没有任何突变的落点已漂移。

    死锁 = 该突变**永不施加** = 那条锁零覆盖，而 harness 整体仍可能报"通过"。
    新增/重构生产代码后若忘了同步 harness 落点，这条会红 —— 这正是 T-1 缺的那个消费者。
    """
    if result.dead:
        lines = "\n".join(
            f"  · {name} #{idx} → {path}\n      落点: {snippet}"
            for name, idx, path, snippet in result.dead)
        pytest.fail(
            f"{len(result.dead)} 条突变落点已漂移（该锁零覆盖，harness 却仍可能报通过）：\n"
            f"{lines}\n\n"
            f"修法：更新对应 harness 的 `old` 字面量至当前源码；若机制已重构，"
            f"把锁**重新对着新的单一事实源**写，别只改字符串。\n"
            f"诊断：.venv/bin/python scripts/harness_landing_audit.py")


def test_no_nonunique_landing_points(result):
    """落点出现 ≥2 次 ⇒ `replace(old, new, 1)` 只改第一处 ⇒ 突变语义不等价，该锁实际未验证。"""
    if result.nonuniq:
        lines = "\n".join(
            f"  · {name} #{idx} → {path}（{n} 次）\n      落点: {snippet}"
            for name, idx, path, snippet, n in result.nonuniq)
        pytest.fail(
            f"{len(result.nonuniq)} 条突变落点非唯一（只改第一处，突变不等价）：\n{lines}\n\n"
            f"修法：把落点扩到含唯一上下文的更长片段。")


def test_undecidable_count_is_bounded(result):
    """非字面量落点（变量拼接）静态判不了 ⇒ 它们**逃出**本闸的覆盖。

    不禁止（个别 harness 用计算字符串有其理由），但**设上界**：数量失控意味着本闸的
    有效覆盖面在悄悄缩小 —— 这正是「为漏项造的兜底网自己也会有缺口」那一类，故把它
    显式钉住而不是默默放过。
    """
    assert len(result.undecidable) <= 8, (
        f"静态判不了的落点有 {len(result.undecidable)} 条，超出上界 ⇒ 本闸覆盖面在缩小：\n"
        + "\n".join(f"  · {n} #{i}: {why}" for n, i, why in result.undecidable))


def test_audit_result_ok_flag_agrees_with_details(result):
    """`ok` 是 CLI 退出码与本测试共用的判据 —— 两者不得分叉（同一事实源）。"""
    assert result.ok == (not result.dead and not result.nonuniq)
