"""Token 用量统计：内存累加 + 批量 upsert + 云端/本地/每项目聚合（mock pool，不碰真库）。"""
from __future__ import annotations

import contextlib

import swarm.models.router as R
from swarm.models import usage_tracker as U


# ── 内存累加 ──
def test_record_accumulates_and_ignores_zero():
    _clear()
    U.record("p1", "cloud", "siliconflow", "GLM-5.2", 100, 50)
    U.record("p1", "cloud", "siliconflow", "GLM-5.2", 10, 5)
    U.record("p1", "local", "MiniMax", "M2.7", 200, 0)
    U.record("p1", "cloud", "x", "y", 0, 0)      # 零 usage → 忽略
    U.record(None, "cloud", "x", "y", -3, -1)    # 负 → 忽略
    assert len(U._buffer) == 2
    # slot = [prompt, completion, calls, duration_ms]
    assert U._buffer[("p1", "cloud", "siliconflow", "GLM-5.2")] == [110, 55, 2, 0]
    assert U._buffer[("p1", "local", "MiniMax", "M2.7")] == [200, 0, 1, 0]


def test_record_accumulates_duration():
    _clear()
    U.record("p1", "cloud", "sf", "m", 10, 5, duration_ms=1200)
    U.record("p1", "cloud", "sf", "m", 10, 5, duration_ms=800)
    assert U._buffer[("p1", "cloud", "sf", "m")] == [20, 10, 2, 2000]


def test_record_kind_normalized_and_none_project_empty():
    _clear()
    U.record(None, "CLOUD", "p", "m", 1, 1)
    assert ("", "cloud", "p", "m") in U._buffer


# ── _extract_token_usage 两种返回形态 ──
def test_extract_usage_shapes():
    class _Msg:
        def __init__(self, um): self.message = type("X", (), {"usage_metadata": um})()

    class _Res:
        def __init__(self, llm_output=None, gens=None):
            self.llm_output = llm_output
            self.generations = gens or []

    assert R._extract_token_usage(_Res(llm_output={"token_usage": {
        "prompt_tokens": 7, "completion_tokens": 3}})) == (7, 3)
    assert R._extract_token_usage(_Res(gens=[[_Msg({
        "input_tokens": 11, "output_tokens": 4})]])) == (11, 4)
    assert R._extract_token_usage(_Res()) == (0, 0)


# ── flush upsert + 聚合读取（mock pool）──
class _FakeCursor:
    def __init__(self, rows_by_call): self._rows = rows_by_call; self.executed = []; self.many = []; self._last = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("SELECT"):
            self._last = self._rows.pop(0) if self._rows else []
    def executemany(self, sql, seq): self.many.append((sql, list(seq)))
    def fetchall(self): return self._last or []


def _clear():
    U._buffer.clear(); U._lat_buffer.clear()


class _FakeConn:
    def __init__(self, cur): self._cur = cur
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self._cur


def _fake_pool(cur):
    class _P:
        def connection(self): return _FakeConn(cur)
    return lambda: _P()


def test_flush_issues_upserts(monkeypatch):
    _clear()
    U._table_ready = True  # 跳过建表
    U.record("p1", "cloud", "sf", "GLM", 100, 50)
    U.record("p2", "local", "mm", "M2", 20, 0)
    cur = _FakeCursor([])
    monkeypatch.setattr(U, "_pool", _fake_pool(cur))
    U.flush()
    upserts = [p for s, p in cur.executed if "INSERT INTO llm_token_usage" in s]
    assert len(upserts) == 2, "两个聚合键各一条 upsert"
    # total_tokens = prompt+completion 正确算入
    glm = next(p for p in upserts if p[3] == "GLM")
    assert glm[4] == 100 and glm[5] == 50 and glm[6] == 150 and glm[7] == 1
    assert not U._buffer, "flush 成功后缓冲清空"


def test_flush_remerges_on_db_failure(monkeypatch):
    _clear()
    U._table_ready = True
    U.record("p1", "cloud", "sf", "GLM", 100, 50)

    def _boom():
        class _P:
            def connection(self): raise RuntimeError("db down")
        return _P()
    monkeypatch.setattr(U, "_pool", _boom)
    U.flush()
    assert U._buffer.get(("p1", "cloud", "sf", "GLM")) == [100, 50, 1, 0], "落库失败应合并回缓冲，不丢数据"


def test_get_stats_aggregates_kind_and_projects(monkeypatch):
    _clear()
    U._table_ready = True
    # SELECT 顺序：① GROUP BY kind(token+累计耗时) ② 每项目 ③ 最近 N 次延迟(kind,sum,count)
    # ③ 用【最近 N 次滑动均】覆盖①的累计均：cloud 6000/2=3000(≠累计 2000，证明覆盖)、local 1000/2=500
    cur = _FakeCursor([
        [("cloud", 1000, 400, 1400, 10, 20000), ("local", 500, 0, 500, 5, 2500)],
        [("p1", "项目甲", 1400, 0, 1400, 10), ("p2", "项目乙", 0, 500, 500, 5)],
        [("cloud", 6000, 2), ("local", 1000, 2)],
    ])
    monkeypatch.setattr(U, "_pool", _fake_pool(cur))
    monkeypatch.setattr(U, "flush", lambda: None)  # 隔离 flush
    out = U.get_token_usage_stats()
    assert out["by_kind"]["cloud"]["total_tokens"] == 1400
    assert out["by_kind"]["local"]["total_tokens"] == 500
    # 平均延迟=最近 N 次滑动均(覆盖累计)：cloud 3000(非累计 2000)、local 500
    assert out["by_kind"]["cloud"]["avg_latency_ms"] == 3000
    assert out["by_kind"]["local"]["avg_latency_ms"] == 500
    assert out["by_kind"]["cloud"]["recent_calls"] == 2
    assert out["latency_window"] == U._RECENT_N
    assert out["grand_total"]["total_tokens"] == 1900
    assert out["grand_total"]["call_count"] == 15
    assert out["grand_total"]["avg_latency_ms"] == 1750  # (6000+1000)/(2+2) 最近窗口
    assert len(out["per_project"]) == 2
    assert out["per_project"][0]["project_name"] == "项目甲"
    assert out["per_project"][0]["cloud_tokens"] == 1400
