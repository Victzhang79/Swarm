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
def test_create_user_returning_includes_must_change(monkeypatch):
    """★批25 GS-5w 换锁★ 原命题=「create_user 的 INSERT...RETURNING 必须带回
    must_change_password 且正确映射进 SwarmUser」——旧版断 SQL 字面量（列被删而
    测试仍绿）。行为级：mock cursor 按【实际执行的 SQL 的 RETURNING 列清单】造行
    （列被删/改名/错位时行随之变形），真调生产 store.create_user 断行映射。
    删什么会变红：RETURNING 删掉该列 → 行变 5 列 → _row_to_user 兜底 False → 红；
    列序错位 → 各列哨兵值互异 → id/username 对齐断言红。"""
    import re

    from swarm.auth import store

    executed: list[str] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            executed.append(sql)
            # 从【运行时真SQL】解析 RETURNING 列清单造行——不抄字面量，SQL 变了行就变
            m = re.search(r"RETURNING\s+(.+?)\s*;?\s*$", sql, re.S | re.I)  # 尾部容许分号（批25 R1 hunter L5：否则末列带 ";" 冤红）
            assert m, "create_user 的 INSERT 必须带 RETURNING（否则行映射无从谈起）"
            cols = [c.strip() for c in m.group(1).split(",")]
            sentinel = {"id": "sent-id", "username": "sent-name",
                        "display_name": "sent-display", "global_role": "sent-role",
                        "api_token": None, "must_change_password": True}
            self._row = tuple(sentinel.get(c) for c in cols)

        def fetchone(self):
            return self._row

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    monkeypatch.setattr(store, "_pooled_conn", lambda conn_str=None: _Conn())
    user = store.create_user(username="u", password="pw123456", must_change_password=True)
    assert executed and "INSERT INTO swarm_users" in executed[0]
    assert user.must_change_password is True, \
        "RETURNING 必须带回 must_change_password 并映射进 SwarmUser（N-24）"
    assert (user.id, user.username) == ("sent-id", "sent-name"), \
        "列序错位会把值映错字段（哨兵互异就是为抓这个）"


# ── N-19：空 diff 短路必须同时考虑 build/test/verify 命令 ──
def test_l1_empty_diff_shortcircuit_considers_all_commands(tmp_path):
    """★批25 GS-5w 换锁★ 原命题=「空 diff + harness 仅有 build/test 命令（无 verify）
    时绝不被空 diff 短路误放行」——旧版断 _has_build/_has_test 变量名与条件字面。
    行为级：真跑生产 run_l1_pipeline，空 diff + harness 只带 build_command →
    短路绝不得触发，build 闸必须真跑（标记文件 + l1_2_1_build_ok 双证）。
    对照面（先自证前提）：harness 无任何命令时短路必须触发（BENIGN no-op），
    证明夹具确实站在短路分支上。
    删什么会变红：短路条件退回只看 verify_commands → 本场景被短路 → 标记不存在
    + note=='no diff changes' → 红。"""
    from swarm.types import FileScope, NotRunKind, SubTask, TaskHarness
    from swarm.worker import l1_pipeline

    def _st(harness):
        return SubTask(id="st-1", description="t",
                       scope=FileScope(writable=["src/x.py"]), harness=harness)

    marker = tmp_path / ".l1_build_ran"
    _ok, details = l1_pipeline.run_l1_pipeline(
        str(tmp_path),
        _st(TaskHarness(language="python", build_command=f"touch {marker}")),
        "", timeout=60)
    assert marker.exists(), \
        "空 diff + 有 build_command 时短路 = 验收被静默跳过（N-19 的病）"
    assert details.get("l1_2_1_build_ok") is True, "build 闸必须真跑且真过"
    assert details.get("note") != "no diff changes", "有验收命令时绝不许走空 diff 短路"
    assert details.get("not_run_kind") != NotRunKind.BENIGN.value

    # 对照面：真空 diff 且无任何验收命令 → 短路必须触发（自证夹具站在短路分支上）
    ok2, d2 = l1_pipeline.run_l1_pipeline(
        str(tmp_path), _st(TaskHarness(language="python")), "", timeout=60)
    assert ok2 is True and d2.get("note") == "no diff changes"
    assert d2.get("not_run_kind") == NotRunKind.BENIGN.value


# ── N-26：analyze 缺 complexity 键回退 MEDIUM（不崩到泛 except）──
def test_analyze_missing_complexity_falls_back(monkeypatch):
    """★批25 GS-5w 换锁★ 原命题=「LLM 输出 JSON 合法但缺 complexity 键 → 回退 medium
    而非 KeyError 崩进泛 except」——旧版断两处源码字面量。行为级：真调生产 analyze
    （project_id="" 跳过知识检索/PG；mock brain LLM 返回缺 complexity 键的合法 JSON），
    断复杂度回退 MEDIUM 且 degraded_reasons 不带「analyze LLM 调用失败」签名
    （那是泛 except 分支的机读指纹，带上=谎报基建故障）。
    删什么会变红：N-26 回退与 B1 形状回退都删 → KeyError 落泛 except → 降级签名出现。
    残余边界（如实登记）：单删 N-26 时 B1 model_validate 回退仍兜住 medium——B1 是
    独立锁住的既有行为，本测试锁的可观测面是「不崩进泛 except」。"""
    import asyncio

    from swarm.brain import nodes
    from swarm.types import Complexity

    class _NoComplexityLLM:
        async def ainvoke(self, messages):
            class _R:
                content = ('{"reasoning": "缺少复杂度字段", "key_risks": [],'
                           ' "suggested_subtask_count": 2}')
            return _R()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _NoComplexityLLM())
    out = asyncio.run(nodes.analyze({
        "task_description": "实现用户登录功能",
        "project_id": "",
    }))
    assert out["complexity"] == Complexity.MEDIUM, \
        f"缺 complexity 键必须回退 MEDIUM（N-26）: {out['complexity']}"
    assert not any("analyze LLM 调用失败" in str(d)
                   for d in out.get("degraded_reasons") or []), \
        "缺键回绝绝不能崩进泛 except（会带上『LLM 调用失败』降级签名=谎报基建故障）"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
