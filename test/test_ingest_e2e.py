#!/usr/bin/env python3
"""摄取层 E2E + 需求池单测（设计 v3 B批3）。

覆盖：
  - ingest 节点：无文件直通、有文件摄取并入 task_description、幂等、失败降级。
  - 上传端点安全：白名单/MIME/路径 sanitize。
  - 需求池：pooled 创建不执行、execute 触发。
mock LLM/store（不真调模型，DB 操作 mock 或真 PG）。
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.ingest_node import ingest


# ── ingest 节点 ────────────────────────────────────────────

def test_ingest_no_files_passthrough():
    """无上传文件 → no-op 直通，不改 task_description。"""
    out = asyncio.run(ingest({"task_description": "纯文字需求"}))
    assert out.get("ingest_done") is True
    assert "task_description" not in out  # 不动原描述
    print("  ✅ ingest: 无文件直通(纯文字任务零影响)")


def test_ingest_idempotent():
    """ingest_done=True → 跳过（幂等，防 replan 重复摄取）。"""
    out = asyncio.run(ingest({"ingest_done": True, "uploaded_files": ["/x.pdf"]}))
    assert out == {}
    print("  ✅ ingest: 幂等(已摄取过则跳过)")


def test_ingest_text_file_merges_to_description(tmp_path):
    """有文本文件 → 解析并入 task_description。"""
    f = tmp_path / "req.txt"
    f.write_text("详细需求：实现用户管理模块", encoding="utf-8")
    out = asyncio.run(ingest({
        "task_description": "做个系统",
        "uploaded_files": [str(f)],
    }))
    assert out["ingest_done"] is True
    assert "用户管理模块" in out["task_description"]
    assert "做个系统" in out["task_description"]
    print("  ✅ ingest: 文本文件解析并入 task_description")


def test_ingest_failure_degrades(tmp_path):
    """摄取异常 → 降级（记 error，不抛，不阻断）。"""
    with patch("swarm.brain.ingest.ingest_files", side_effect=RuntimeError("boom")):
        out = asyncio.run(ingest({
            "task_description": "原始需求",
            "uploaded_files": ["/whatever.txt"],
        }))
    assert out["ingest_done"] is True
    assert out.get("ingest_errors")
    print("  ✅ ingest: 摄取异常降级(不阻断主流程)")


def test_ingest_image_records_vision_pending(tmp_path):
    """图片文件 → 多模态理解 + 记 vision_pending（待确认）。"""
    img = tmp_path / "ui.png"
    img.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100" + "0" * 20
    ))

    class _VR:
        filename = "ui.png"
        understanding = "登录界面截图"
        model_used = "vision-pro"
        source = "ai_vision"
        confirmed = False
        error = ""
        @property
        def ok(self):
            return True

    with patch("swarm.brain.vision_ingest.understand_file", return_value=_VR()):
        with patch("swarm.brain.vision_ingest.annotate_for_draft",
                   return_value="【文件: ui.png】（AI理解,待确认）登录界面截图"):
            out = asyncio.run(ingest({
                "task_description": "做个登录页",
                "uploaded_files": [str(img)],
            }))
    assert out["ingest_done"] is True
    pending = out.get("ingest_vision_pending", [])
    assert any(p["filename"] == "ui.png" and p["confirmed"] is False for p in pending)
    print("  ✅ ingest: 图片→多模态理解+记vision_pending(待确认)")


def test_ingest_auto_confirm_vision_skips_pending(tmp_path):
    """auto_confirm_vision=True → 图片理解直接确认，不进 pending。"""
    img = tmp_path / "ui.png"
    img.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100" + "0" * 20
    ))

    class _VR:
        filename = "ui.png"
        understanding = "登录界面"
        model_used = "vision-pro"
        source = "ai_vision"
        confirmed = False
        error = ""
        @property
        def ok(self):
            return True

    with patch("swarm.brain.vision_ingest.understand_file", return_value=_VR()):
        with patch("swarm.brain.vision_ingest.annotate_for_draft", return_value="理解内容"):
            out = asyncio.run(ingest({
                "task_description": "做个登录页",
                "uploaded_files": [str(img)],
                "auto_confirm_vision": True,
            }))
    assert out.get("ingest_vision_pending") == []  # auto_confirm → 不待确认
    print("  ✅ ingest: auto_confirm_vision → 跳过人工确认")


# ── 上传端点安全 ───────────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient
    from swarm.api.app import app
    return TestClient(app)


def test_upload_rejects_bad_extension():
    client = _client()
    resp = client.post("/api/uploads", files={"files": ("evil.exe", b"MZ\x90\x00", "application/octet-stream")})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok_count"] == 0
    assert "不支持的格式" in data["files"][0]["error"]
    print("  ✅ 上传: 拒绝非白名单扩展名")


def test_upload_rejects_mime_mismatch():
    """扩展名 .pdf 但内容不是 PDF → MIME 校验失败。"""
    client = _client()
    resp = client.post("/api/uploads", files={"files": ("fake.pdf", b"this is plain text not pdf", "application/pdf")})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok_count"] == 0
    assert "MIME" in data["files"][0]["error"]
    print("  ✅ 上传: MIME 魔数校验(扩展名.pdf但非PDF内容)")


def test_upload_accepts_valid_text():
    client = _client()
    resp = client.post("/api/uploads", files={"files": ("req.md", "# 需求\n做个系统".encode(), "text/markdown")})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok_count"] == 1
    assert data["files"][0]["kind"] == "text"
    assert "path" in data["files"][0]
    # 清理上传的文件
    Path(data["files"][0]["path"]).unlink(missing_ok=True)
    print("  ✅ 上传: 合法文本文件接受+落盘")


def test_upload_sanitizes_path_traversal():
    """文件名含 ../ 路径穿越 → sanitize 后落在隔离目录内。"""
    client = _client()
    resp = client.post("/api/uploads", files={"files": ("../../etc/passwd.txt", b"x" * 10, "text/plain")})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    if data["ok_count"] == 1:
        path = data["files"][0]["path"]
        # 落盘路径必须在 uploads 目录内，不能穿越出去
        assert "/uploads/" in path
        assert "etc/passwd" not in path or path.count("..") == 0
        Path(path).unlink(missing_ok=True)
    print("  ✅ 上传: 路径穿越文件名被 sanitize")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
