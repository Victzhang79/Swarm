"""SWARM_CTO_GUIDE Batch E/F 回归测试 — P1 架构根因 + N-tail 正确性。

覆盖：P1-DEBT-04 KB point ID 单一来源、N-24 create_user RETURNING、N-19 空 diff 短路、
N-26 缺 complexity 键回退。
"""
from __future__ import annotations

import inspect


# ── P1-DEBT-04：preprocess 与 semantic 共用同一 point ID 方案 ──
def test_make_point_id_stable_and_shared():
    from swarm.knowledge.semantic_index import make_point_id

    a = make_point_id("p1", "pkg/m.py", 10, "def foo(): return 1")
    b = make_point_id("p1", "pkg/m.py", 10, "def foo(): return 1")
    assert a == b, "同 (project,file,line) 必须产同一 ID"
    assert a != make_point_id("p1", "pkg/m.py", 11, "def foo(): return 1")  # 行不同→不同
    # A-P1-19：ID 按 (project,file,line)，content 不参与 → 同键不同内容产同一 ID，
    # 这才让 codegraph(签名|文档|名) 与 semantic(分块原文) 两路径对同一逻辑 chunk 真正去重。
    assert a == make_point_id("p1", "pkg/m.py", 10, "def bar(): return 2")  # 内容不同→仍同 ID
    # D13：project_id 参与 key → 跨项目同 (file,line) 不同 ID（不互相覆盖）
    assert a != make_point_id("p2", "pkg/m.py", 10, "def foo(): return 1")
    assert isinstance(a, str) and len(a) == 36  # uuid5 字符串


def test_make_point_id_cross_path_same_chunk():
    """A-P1-19：codegraph 与 semantic 对同一 (file,line) 即便喂不同 content 也产同一 ID。"""
    from swarm.knowledge.semantic_index import make_point_id

    codegraph_content = "def foo(a, b): ... | 计算两数之和 | foo"   # 签名|文档|名
    semantic_content = "def foo(a, b):\n    return a + b\n"        # 分块原文
    assert make_point_id("p1", "svc/x.py", 42, codegraph_content) == make_point_id(
        "p1", "svc/x.py", 42, semantic_content
    )


def test_preprocess_uses_shared_point_id():
    """preprocess 不再用独立 blake2b int 方案（与 semantic 不相交）。"""
    from swarm.project import preprocess

    src = inspect.getsource(preprocess)
    assert "make_point_id(" in src, "preprocess 应改用共享 make_point_id"
    # 旧 blake2b int point ID 方案应已移除
    assert "hashlib.blake2b(point_id" not in src


# ── N-24：create_user RETURNING 含 must_change_password ──
def test_create_user_returning_includes_must_change():
    from swarm.auth import store

    src = inspect.getsource(store.create_user)
    assert "RETURNING id, username, display_name, global_role, api_token, must_change_password" in src


# ── N-19：空 diff 短路必须同时考虑 build/test/verify 命令 ──
def test_l1_empty_diff_shortcircuit_considers_all_commands():
    from swarm.worker import l1_pipeline

    src = inspect.getsource(l1_pipeline.run_l1_pipeline) if hasattr(l1_pipeline, "run_l1_pipeline") else inspect.getsource(l1_pipeline)
    # 短路条件须引用 build_command/test_command（不只 verify_commands）
    assert "_has_build" in src and "_has_test" in src
    assert "not (_has_verify or _has_build or _has_test)" in src


# ── N-26：analyze 缺 complexity 键回退 MEDIUM（不崩到泛 except）──
def test_analyze_missing_complexity_falls_back():
    from swarm.brain import nodes

    src = inspect.getsource(nodes)
    assert 'result["complexity"] = "medium"' in src
    assert 'if not result.get("complexity")' in src


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
