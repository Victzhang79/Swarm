"""12.16 修复回归测试：kb_pending_embeddings 死信队列可观测 + requeue。

问题：embedding 长期不可用 → kb_pending_embeddings retry_count 累积，>=10 视为
永久失败不再自动重试，但此前无 API 暴露、无法手动恢复（运维盲区）。

修复：新增
- GET  /api/projects/{id}/knowledge/pending-embeddings  → 列出含 dead 标记
- POST /api/projects/{id}/knowledge/pending-embeddings/requeue → dead 条目 retry_count 清零

触真实 PG。测试铁律：_test_ 隔离 project_id + try/finally 清理。RBAC-off(conftest 默认)。
"""

from __future__ import annotations

import uuid

import psycopg
from fastapi.testclient import TestClient

from swarm.config.settings import DatabaseConfig

_PID = f"_test_12_16_{uuid.uuid4().hex[:8]}"


def _conn():
    return psycopg.connect(DatabaseConfig().postgres_uri, autocommit=True)


def _seed():
    with _conn() as conn:
        with conn.cursor() as cur:
            # 项目行（_validate_project 需要）
            cur.execute(
                "INSERT INTO projects (id, name, path) VALUES (%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                (_PID, "t16", f"/tmp/{_PID}"),
            )
            # 一条 dead(retry_count=10) + 一条正常(retry_count=2)
            cur.execute(
                "INSERT INTO kb_pending_embeddings (project_id, file_path, retry_count) VALUES (%s,%s,%s)"
                " ON CONFLICT (project_id, file_path) DO UPDATE SET retry_count=EXCLUDED.retry_count",
                (_PID, "dead.py", 10),
            )
            cur.execute(
                "INSERT INTO kb_pending_embeddings (project_id, file_path, retry_count) VALUES (%s,%s,%s)"
                " ON CONFLICT (project_id, file_path) DO UPDATE SET retry_count=EXCLUDED.retry_count",
                (_PID, "alive.py", 2),
            )


def _cleanup():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kb_pending_embeddings WHERE project_id=%s", (_PID,))
            cur.execute("DELETE FROM projects WHERE id=%s", (_PID,))


def test_pending_embeddings_observe_and_requeue():
    from swarm.api.app import app

    _seed()
    try:
        client = TestClient(app)

        # 1) 列表：dead 标记正确
        r = client.get(f"/api/projects/{_PID}/knowledge/pending-embeddings")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["total"] == 2, data
        assert data["dead"] == 1, data
        assert data["pending"] == 1, data
        dead_item = next(i for i in data["items"] if i["file_path"] == "dead.py")
        assert dead_item["dead"] is True
        alive_item = next(i for i in data["items"] if i["file_path"] == "alive.py")
        assert alive_item["dead"] is False

        # 2) requeue：dead 条目 retry_count 清零
        r2 = client.post(f"/api/projects/{_PID}/knowledge/pending-embeddings/requeue")
        assert r2.status_code == 200, r2.text
        assert r2.json()["requeued"] == 1, r2.json()

        # 3) 再查：dead 归零
        r3 = client.get(f"/api/projects/{_PID}/knowledge/pending-embeddings")
        assert r3.json()["dead"] == 0, r3.json()
    finally:
        _cleanup()


if __name__ == "__main__":
    try:
        test_pending_embeddings_observe_and_requeue()
        print("  ✅ test_pending_embeddings_observe_and_requeue")
        print("\n=== 12.16 pending-embeddings observability: 1/1 passed ===")
    except AssertionError as e:
        print(f"  ❌ {e}")
        raise
