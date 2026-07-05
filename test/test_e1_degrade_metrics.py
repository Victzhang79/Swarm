"""E1：降级路径分类计数 + /api/metrics 暴露（行为测试，不 getsource）。"""
from __future__ import annotations

from swarm.infra.degrade import degrade_counts, record_degrade, reset_degrade_counts


def test_registry_counts_by_category():
    reset_degrade_counts()
    record_degrade("a.b")
    record_degrade("a.b")
    record_degrade("c.d")
    counts = degrade_counts()
    assert counts["a.b"] == 2
    assert counts["c.d"] == 1
    reset_degrade_counts()
    assert degrade_counts() == {}


def test_empty_category_bucketed_unknown():
    reset_degrade_counts()
    record_degrade("")
    assert degrade_counts()["unknown"] == 1
    reset_degrade_counts()


def test_snapshot_is_copy_not_live_ref():
    reset_degrade_counts()
    record_degrade("x")
    snap = degrade_counts()
    record_degrade("x")
    assert snap["x"] == 1, "degrade_counts 应返回快照副本，不随后续记录变动"
    reset_degrade_counts()


def test_metrics_endpoint_exposes_degrade_counter():
    from fastapi.testclient import TestClient

    from swarm.api.app import app

    reset_degrade_counts()
    record_degrade("test.e1_probe")
    record_degrade("test.e1_probe")
    client = TestClient(app)
    resp = client.get("/api/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.text
    assert "# TYPE swarm_degrade_total counter" in body
    assert 'swarm_degrade_total{category="test.e1_probe"} 2' in body
    reset_degrade_counts()
