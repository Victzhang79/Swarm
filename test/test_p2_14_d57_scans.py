"""P2-14 D57（安全子集）：重复全量扫描收敛。

- tech_design 事实采集 _gather_project_facts：120s TTL memo（review×3/replan 不重扫整树）。
- 点名文件核验 _verify_named_files_exist：30s TTL memo（同上，且判定结果不变）。
- L1 manifest 在场性 _manifest_present：单次 L1 run 内缓存（5-8 趟沙箱 find 收敛），
  run 入口与清单推送路径失效。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def _clean_memos():
    from swarm.brain import planning_nodes as pn
    from swarm.worker import l1_pipeline as l1

    pn._FACTS_MEMO.clear()
    pn._VERIFY_MEMO.clear()
    l1._invalidate_manifest_cache()
    yield
    pn._FACTS_MEMO.clear()
    pn._VERIFY_MEMO.clear()
    l1._invalidate_manifest_cache()


def _proj(tmp_path):
    (tmp_path / "src" / "controller").mkdir(parents=True)
    (tmp_path / "src" / "controller" / "UserController.java").write_text("class A {}")
    (tmp_path / "pom.xml").write_text("<project/>")
    return str(tmp_path)


def test_d57_gather_facts_memo_single_walk(tmp_path, _clean_memos, monkeypatch):
    import os as _os

    from swarm.brain import planning_nodes as pn

    p = _proj(tmp_path)
    calls = {"n": 0}
    real_walk = _os.walk

    def counting_walk(*a, **kw):
        calls["n"] += 1
        return real_walk(*a, **kw)

    monkeypatch.setattr(pn.os if hasattr(pn, "os") else _os, "walk", counting_walk)
    out1 = pn._gather_project_facts(p)
    out2 = pn._gather_project_facts(p)
    assert out1 == out2 and "pom.xml" in out1
    assert calls["n"] == 1  # 第二次命中 memo，不再整树 walk

    # TTL 过期 → 重扫（人为老化缓存条目）
    key = next(iter(pn._FACTS_MEMO))
    ts, val = pn._FACTS_MEMO[key]
    pn._FACTS_MEMO[key] = (ts - pn._FACTS_MEMO_TTL_S - 1, val)
    pn._gather_project_facts(p)
    assert calls["n"] == 2


def test_d57_verify_named_files_memo_and_correctness(tmp_path, _clean_memos, monkeypatch):
    import os as _os

    from swarm.brain import planning_nodes as pn

    p = _proj(tmp_path)
    desc = "请修改 UserController.java 并新建 GhostService.java"
    calls = {"n": 0}
    real_walk = _os.walk

    def counting_walk(*a, **kw):
        calls["n"] += 1
        return real_walk(*a, **kw)

    monkeypatch.setattr(_os, "walk", counting_walk)
    r1 = pn._verify_named_files_exist(desc, p)
    r2 = pn._verify_named_files_exist(desc, p)
    by_file = {r["file"]: r for r in r1}
    assert by_file["UserController.java"]["exists"] is True   # 判定正确性不变
    assert by_file["GhostService.java"]["exists"] is False
    assert r1 == r2
    assert calls["n"] == 1                                    # memo 生效
    # 不同 description → 不吃错缓存
    pn._verify_named_files_exist("另一个需求 Other.java", p)
    assert calls["n"] == 2
    # 缓存返回浅拷贝：调用方改结果不污染缓存
    r2[0]["exists"] = "tampered"
    r3 = pn._verify_named_files_exist(desc, p)
    assert all(r["exists"] != "tampered" for r in r3)


def test_d57_manifest_present_cached_within_l1_run(_clean_memos, monkeypatch):
    from swarm.worker import l1_pipeline as l1

    calls = {"n": 0}

    class _CR:
        stdout = "/workspace/pom.xml\n"
        success = True
        error = None

    class _Mgr:
        def run_command(self, sandbox, cmd, timeout=20, **kw):
            calls["n"] += 1
            return _CR()

    class _SB:
        sandbox_id = "sb-1"

    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (_SB(), _Mgr(), "/workspace"))

    assert l1._manifest_present(("pom.xml",), "/proj") is True
    assert l1._manifest_present(("pom.xml",), "/proj") is True
    assert calls["n"] == 1                                    # 同 run 内第二次命中缓存
    assert l1._manifest_present(("go.mod",), "/proj") is True
    assert calls["n"] == 2                                    # 不同 manifests = 不同键
    l1._invalidate_manifest_cache()                           # 新一次 L1 run / 清单推送后
    assert l1._manifest_present(("pom.xml",), "/proj") is True
    assert calls["n"] == 3                                    # 失效后重探


def test_d57_manifest_probe_exception_not_cached(_clean_memos, monkeypatch):
    from swarm.worker import l1_pipeline as l1

    calls = {"n": 0}

    class _Mgr:
        def run_command(self, sandbox, cmd, timeout=20, **kw):
            calls["n"] += 1
            raise RuntimeError("sandbox flap")

    class _SB:
        sandbox_id = "sb-2"

    monkeypatch.setattr(l1, "_sandbox_ctx", lambda: (_SB(), _Mgr(), "/workspace"))
    assert l1._manifest_present(("pom.xml",), "/proj") is False  # 保守 False（旧行为）
    assert l1._manifest_present(("pom.xml",), "/proj") is False
    assert calls["n"] == 2                                       # 异常不缓存，下次重探
