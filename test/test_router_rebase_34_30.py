"""audit #34/#30 修复回归测试。

#34 ModelRouter.get_alternate_llm_for_subtask 公共方法（替代 dispatch 调私有方法）。
#30 rebase 独立计数上限防无限循环（不污染 retry 计数）。
"""

from __future__ import annotations

from swarm.models.router import ModelRouter


def test_34_alternate_public_method_exists_and_returns_pair():
    """公共方法存在，返回 (llm, model_name) 二元组，model_name 非空。"""
    r = ModelRouter()
    llm, model_name = r.get_alternate_llm_for_subtask("medium", "text")
    assert llm is not None
    assert isinstance(model_name, str) and model_name


def test_34_no_private_call_in_dispatch():
    """dispatch 不再直接调 ModelRouter 私有方法（源码层断言封装恢复）。"""
    import inspect
    import swarm.brain.nodes as nodes
    src = inspect.getsource(nodes._dispatch_to_worker)
    assert "router._resolve_route" not in src, "dispatch 不应再调私有 _resolve_route"
    assert "router._get_provider_for_model" not in src, "dispatch 不应再调私有 _get_provider_for_model"
    assert "get_alternate_llm_for_subtask" in src


def test_30_rebase_count_field_in_state():
    """BrainState 含独立 rebase 计数字段（与 retry 计数解耦）。"""
    from swarm.brain.state import BrainState
    ann = BrainState.__annotations__
    assert "subtask_rebase_counts" in ann
    # retry 计数仍独立存在，未被 rebase 污染
    assert "subtask_retry_counts" in ann


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  💥 {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n=== #34/#30: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
