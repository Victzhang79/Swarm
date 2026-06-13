#!/usr/bin/env python3
"""多模态摄取单测（设计 v3 B批2）。

mock LLM（不真调模型/网络）+ 真实图片/PDF 生成。
覆盖：选模型、图片→data URL、扫描PDF渲染、理解成功/失败、防幻觉标注、无模型降级。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import vision_ingest as vi


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100" + "0" * 20
)


def _make_png(tmp_path: Path, name: str = "img.png") -> Path:
    p = tmp_path / name
    p.write_bytes(_TINY_PNG)
    return p


def _make_pdf_with_image(tmp_path: Path, name: str = "scan.pdf") -> Path:
    """造一个有页面的 PDF（用于测渲染为图）。"""
    import fitz
    p = tmp_path / name
    doc = fitz.open()
    doc.new_page()
    doc.save(str(p))
    doc.close()
    return p


# ── 选模型 ─────────────────────────────────────────────────

def test_select_vision_model_from_capabilities():
    with patch("swarm.models.router.ModelRouter") as MR:
        inst = MR.return_value
        inst._multimodal_model_from_capabilities.return_value = "vision-pro"
        assert vi.select_vision_model() == "vision-pro"
    print("  ✅ 选模型: 能力库优先 → vision-pro")


def test_select_vision_model_fallback():
    with patch("swarm.models.router.ModelRouter") as MR:
        inst = MR.return_value
        inst._multimodal_model_from_capabilities.return_value = None
        inst.config.routing_multimodal = "Step-3.7-Flash"
        assert vi.select_vision_model() == "Step-3.7-Flash"
    print("  ✅ 选模型: 能力库空 → 回退写死配置")


# ── data URL / 渲染 ────────────────────────────────────────

def test_image_to_data_url(tmp_path):
    p = _make_png(tmp_path)
    url = vi._image_to_data_url(p)
    assert url.startswith("data:image/png;base64,")
    print("  ✅ data URL: png 编码正确")


def test_render_pdf_pages(tmp_path):
    p = _make_pdf_with_image(tmp_path)
    pngs = vi._render_pdf_pages_to_pngs(p)
    assert len(pngs) >= 1
    assert pngs[0].startswith(b"\x89PNG")  # PNG 魔数
    print("  ✅ 渲染: 扫描PDF页 → PNG字节")


# ── 多模态理解（mock LLM）──────────────────────────────────

def _mock_router(understanding="这是一个登录界面，含用户名和密码输入框"):
    """构造一个 mock ModelRouter，其 LLM.invoke 返回固定理解文本。"""
    router = MagicMock()
    llm = MagicMock()
    resp = MagicMock()
    resp.content = understanding
    llm.invoke.return_value = resp
    router.get_model_by_name.return_value = llm
    return router


def test_understand_image_success(tmp_path):
    p = _make_png(tmp_path)
    with patch("swarm.brain.vision_ingest.select_vision_model", return_value="vision-pro"):
        with patch("swarm.models.router.ModelRouter", return_value=_mock_router()):
            result = vi.understand_file(p, "image")
    assert result.ok
    assert "登录界面" in result.understanding
    assert result.source == "ai_vision"
    assert result.confirmed is False   # 防幻觉：默认未确认
    assert result.model_used == "vision-pro"
    print("  ✅ 理解: 图片成功 + source=ai_vision + confirmed=False")


def test_understand_scanned_pdf(tmp_path):
    p = _make_pdf_with_image(tmp_path)
    with patch("swarm.brain.vision_ingest.select_vision_model", return_value="vision-pro"):
        with patch("swarm.models.router.ModelRouter", return_value=_mock_router("扫描的需求文档")):
            result = vi.understand_file(p, "pdf")
    assert result.ok
    assert "扫描的需求文档" in result.understanding
    print("  ✅ 理解: 扫描PDF渲染+理解成功")


def test_understand_no_model(tmp_path):
    p = _make_png(tmp_path)
    with patch("swarm.brain.vision_ingest.select_vision_model", return_value=None):
        result = vi.understand_file(p, "image")
    assert not result.ok
    assert "无可用多模态模型" in result.error
    print("  ✅ 理解: 无多模态模型 → 优雅降级(记error不抛)")


def test_understand_llm_failure(tmp_path):
    p = _make_png(tmp_path)
    router = MagicMock()
    router.get_model_by_name.side_effect = RuntimeError("模型超时")
    with patch("swarm.brain.vision_ingest.select_vision_model", return_value="vision-pro"):
        with patch("swarm.models.router.ModelRouter", return_value=router):
            result = vi.understand_file(p, "image")
    assert not result.ok
    assert "多模态理解失败" in result.error
    print("  ✅ 理解: LLM调用失败 → 记error不抛")


def test_understand_empty_response(tmp_path):
    p = _make_png(tmp_path)
    with patch("swarm.brain.vision_ingest.select_vision_model", return_value="vision-pro"):
        with patch("swarm.models.router.ModelRouter", return_value=_mock_router("")):
            result = vi.understand_file(p, "image")
    assert not result.ok
    assert "返回空内容" in result.error
    print("  ✅ 理解: 模型返回空 → 记error")


# ── 防幻觉标注 ─────────────────────────────────────────────

def test_annotate_for_draft_ok():
    r = vi.VisionResult(filename="ui.png", understanding="登录界面",
                        model_used="vision-pro")
    text = vi.annotate_for_draft(r)
    assert "AI 视觉理解，待人工确认" in text
    assert "登录界面" in text
    assert "ui.png" in text
    print("  ✅ 标注: 防幻觉标注「AI理解,待确认」")


def test_annotate_for_draft_failed():
    r = vi.VisionResult(filename="bad.png", error="模型超时")
    text = vi.annotate_for_draft(r)
    assert "多模态理解失败" in text
    assert "模型超时" in text
    print("  ✅ 标注: 失败时标注错误原因")


# ── 多模态消息格式（验证发给 LLM 的结构）──────────────────

def test_invoke_vision_message_format(tmp_path):
    """验证发给 LLM 的多模态消息格式正确（text + image_url）。"""
    captured = {}

    def fake_invoke(messages):
        captured["messages"] = messages
        resp = MagicMock()
        resp.content = "理解结果"
        return resp

    router = MagicMock()
    llm = MagicMock()
    llm.invoke.side_effect = fake_invoke
    router.get_model_by_name.return_value = llm

    with patch("swarm.models.router.ModelRouter", return_value=router):
        out = vi._invoke_vision("vision-pro", ["data:image/png;base64,AAAA"])
    assert out == "理解结果"
    content = captured["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png")
    print("  ✅ 消息格式: text + image_url 结构正确")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
