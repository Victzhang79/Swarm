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


# ── T-2：每个 harness 都必须清 pyc ─────────────────────────────────────────

def test_every_harness_clears_pyc():
    """★T-2 的机读闸★ 每个 `scripts/*mutation_check.py` 都必须有 `_clear_pyc`。

    CPython 判 pyc 是否有效看的是源码 **mtime（整秒粒度）+ 字节数**。当「等长突变
    （`len(old)==len(new)`）」与「同秒写入」同时成立时，第二条突变写完 pyc 仍被判有效
    ⇒ 子进程加载的是**上一条**的字节码。双向危害：
      · 「突变后仍绿」→ 冤报"这条测试没牙"（其实跑的根本不是这条突变的代码）
      · 「红的是上一条」→ **假背书**：这条锁其实没被验证，却显示通过
    实测本仓曾有 13 个 harness 缺它、其中 8 条突变等长（#29-3 已统一补齐）。
    ★此闸防的是【下一个】新增 harness 又忘了带★ —— 与落点漂移同理：只修存量不装闸，
    第 14 个会同样静默（本仓已登记「为漏项造的兜底网不能用同一份枚举编」）。
    """
    missing = _load_audit().audit_pyc_clearing()
    if missing:
        pytest.fail(
            f"{len(missing)} 个 harness 缺 `_clear_pyc`（等长突变 + 同秒写入 ⇒ 跑的是"
            f"上一条的字节码，既冤报没牙也可能假背书）：\n"
            + "\n".join(f"  · {n}" for n in missing)
            + "\n\n参考实现：scripts/ph4_mutation_check.py 的 `_clear_pyc`；"
              "每条突变**写入后**与**还原后**都要调一次。")


def test_pyc_audit_actually_scanned_harnesses():
    """★前提锁★：若 glob 写错/读不到文件，`audit_pyc_clearing()` 会返回空列表，
    上面那条断言就恒真＝假绿。故先证它真的看过了足够多的 harness。"""
    mod = _load_audit()
    n = len(list(mod.ROOT.glob("scripts/*mutation_check.py")))
    assert n >= 20, f"只扫到 {n} 个 harness ⇒ glob 或 ROOT 解析坏了，T-2 闸恒真"


def test_equal_length_mutations_are_reported_not_blocked():
    """等长突变本身**完全合法**（配对守卫天然等长，如版本号 8→7），不该当红线。

    这条锁的是"诊断信息在场"而非"数量为零"：拿等长当禁令会误杀大批正当突变；真正的
    防线是 `_clear_pyc` 在场（上一条测的就是它）。此处只确认诊断通道还活着——
    它返回的形状可用，且当前确实有等长条目（若某天变成 0，说明扫描逻辑坏了而非本仓
    真没有等长突变）。
    """
    eq = _load_audit().audit_equal_length_mutations()
    assert isinstance(eq, list)
    assert eq, "等长突变扫描返回空 —— 本仓实测有等长条目，返空说明解析坏了"
    for item in eq:
        assert len(item) == 3, f"形状变了: {item}"
        name, idx, ln = item
        assert isinstance(name, str) and name.endswith(".py")
        assert isinstance(idx, int) and idx >= 1
        assert isinstance(ln, int) and ln >= 0
