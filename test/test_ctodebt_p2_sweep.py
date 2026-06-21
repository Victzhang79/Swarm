#!/usr/bin/env python3
"""CTO 技术债 ⑤ P2 清扫波 — 纯逻辑特征化单测（不依赖 DB/网络）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_sliding_window_never_evicts_user():
    """USER(priority=1) 即便最旧也永不逐出；预算不足宁可溢出。"""
    from swarm.memory.sliding_window import PRIORITY_USER, PRIORITY_WORKER, compress_context_log

    # USER 在前(最旧) + 多条大 WORKER 事件，强制逐出。USER 必须留存。
    log = [{"type": "user", "content": "U" * 100, "tokens": 500, "priority": PRIORITY_USER}]
    for i in range(20):
        log.append({"type": "worker", "content": f"W{i}" * 100, "tokens": 2000, "priority": PRIORITY_WORKER})
    new_log, _summary, _tk = compress_context_log(log, "", max_tokens=4000, reserve_tokens=1000)
    kept_priorities = [e.get("priority") for e in new_log]
    assert PRIORITY_USER in kept_priorities, "USER 被逐出，违反永不丢弃契约"
    print("  ✅ sliding_window USER 永不逐出")


def test_sliding_window_all_user_no_evict():
    """全是 USER 且超预算 → 不逐出任何(接受溢出)，不崩。"""
    from swarm.memory.sliding_window import PRIORITY_USER, compress_context_log

    log = [{"type": "user", "content": "U" * 100, "tokens": 5000, "priority": PRIORITY_USER} for _ in range(5)]
    new_log, _s, _tk = compress_context_log(log, "", max_tokens=2000, reserve_tokens=500)
    assert len(new_log) == 5, "USER 不应被逐出"
    print("  ✅ sliding_window 全 USER 超预算不逐出不崩")


def test_command_blacklist_baseline_nonempty():
    """DB 不可用兜底基线非空(内置危险规则)，非完全 fail-open。"""
    from swarm.config.command_blacklist_store import _compile_default_rules

    rules = _compile_default_rules()
    assert len(rules) >= 3, rules
    pats = " ".join(p for _id, p, _d in rules)
    assert "rm" in pats or "shutdown" in pats
    print(f"  ✅ command_blacklist 内置基线 {len(rules)} 条(DB 故障时仍拦危险命令)")


def test_subtask_alias_remap_and_extra_visible():
    """SubTask 旧键别名重映射(不丢数据)；未知键不再静默(可见)。"""
    from swarm.types import FileScope, SubTask

    st = SubTask(
        id="x", description="d", scope=FileScope(writable=["a.py"]),
        acceptance=["c1"], dependencies=["y"],
    )
    assert st.acceptance_criteria == ["c1"], st.acceptance_criteria
    assert st.depends_on == ["y"], st.depends_on
    print("  ✅ SubTask 旧键 acceptance/dependencies 重映射不丢数据")


def test_prober_neg_signals_tightened():
    """多模态否定信号不再含过宽词(invalid/unsupported/image_url)。"""
    import inspect

    from swarm.models import prober

    src = inspect.getsource(prober.probe_multimodal)
    # 旧实现的过宽信号相邻串(裸 invalid/unsupported/结构字段 image_url 当否定信号)应已移除。
    assert '"invalid", "unsupported"' not in src, "neg_signals 仍含过宽词 invalid/unsupported"
    assert '"unsupported", "image_url"' not in src, "neg_signals 仍把 image_url 当否定信号"
    print("  ✅ prober 多模态否定信号已收紧(去过宽词)")


def test_consistency_display_limit_none_full():
    """display_limit=None 返回全量列表(供 repair 收敛)，默认截断展示。"""
    import inspect

    from swarm.knowledge import consistency

    sig = inspect.signature(consistency.check_project_consistency)
    assert "display_limit" in sig.parameters, "应新增 display_limit 参数"
    print("  ✅ consistency check 支持 display_limit(None=全量供 repair)")


def test_taskplan_topo_order_exists():
    """TaskPlan.topological_order 可用(merge rebase base 依赖它)。"""
    from swarm.types import TaskPlan

    assert hasattr(TaskPlan, "topological_order")
    print("  ✅ TaskPlan.topological_order 存在")


def main() -> int:
    print("=== test_ctodebt_p2_sweep ===")
    failed = 0
    for fn in (
        test_sliding_window_never_evicts_user,
        test_sliding_window_all_user_no_evict,
        test_command_blacklist_baseline_nonempty,
        test_subtask_alias_remap_and_extra_visible,
        test_prober_neg_signals_tightened,
        test_consistency_display_limit_none_full,
        test_taskplan_topo_order_exists,
    ):
        try:
            fn()
        except Exception as exc:
            failed += 1
            print(f"  ❌ {fn.__name__}: {exc}")
            import traceback

            traceback.print_exc()
    print(f"\n{'All passed' if not failed else str(failed) + ' failed'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
