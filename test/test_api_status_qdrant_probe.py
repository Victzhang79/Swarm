"""P0-2（CODEWALK_AUDIT_2026-07-06 批1）：/api/status 知识库分量 Qdrant 探活假绿。

原 bug：api/app.py _check_component("知识库") 末尾
    qdrant_ok_flag = any("qdrant" in d for d in details)
Qdrant 全挂时失败文案 "qdrant unreachable: X" 同样含子串 "qdrant" → flag 恒 True →
/api/status 报 Qdrant 健康（running），与 /api/health/ready 的 _probe_qdrant_ready
真探活漂移；探测块里真正赋值的 qdrant_ok 反而从未被读（noqa: F841 死变量）。
修复：qdrant_ok_flag 改用真实探测结果 qdrant_ok。
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types

# swarm/api/__init__.py 把 FastAPI 实例 re-export 为 app，遮蔽同名子模块 → 用 importlib 拿模块
_app_module = lambda: importlib.import_module("swarm.api.app")  # noqa: E731


def test_status_kb_component_reports_qdrant_down(monkeypatch):
    app_mod = _app_module()

    # 1) 远程 Qdrant 探测：连接异常（模拟 Qdrant 全挂）
    import httpx

    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise ConnectionError("qdrant down")

        async def post(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)

    # 2) 本地文件模式 fallback：~/.swarm/qdrant 不存在（其余路径判定不受影响）
    real_exists = os.path.exists
    monkeypatch.setattr(
        os.path, "exists",
        lambda p: False if "qdrant" in str(p) else real_exists(p),
    )

    # 3) embedding 探测确定性可用——老代码正是在 embed_ok=True 时把假绿放大成 running
    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = object
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    status = asyncio.run(app_mod._check_component("知识库", is_admin=True))
    assert "qdrant unreachable" in status["detail"], status
    assert status["status"] == "degraded", (
        f"Qdrant 全挂 + embedding 可用应报 degraded，实际 {status['status']}"
        "（running=子串启发式假绿复发）"
    )


def test_status_kb_component_running_when_qdrant_up(monkeypatch):
    """对照：Qdrant 真在线时仍应 running（收紧探活不得误伤健康路径）。"""
    app_mod = _app_module()

    import httpx

    class _OkResp:
        status_code = 200

        def json(self):
            return {"result": {"collections": [{"name": "kb"}]}}

    class _OkClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _OkResp()

    monkeypatch.setattr(httpx, "AsyncClient", _OkClient)

    fake = types.ModuleType("fastembed")
    fake.TextEmbedding = object
    monkeypatch.setitem(sys.modules, "fastembed", fake)

    status = asyncio.run(app_mod._check_component("知识库", is_admin=True))
    assert status["status"] == "running", status
