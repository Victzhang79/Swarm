"""多模态需求摄取 — 图片/扫描件理解（设计 v3 B批2，消费 A 能力库）。

把 B批1 标记为 needs_vision 的文件（图片、无文本层的扫描 PDF）交给多模态模型理解，
产出文本描述并入需求草稿。

设计约束：
  - 选模型（A.5）：从能力库挑 supports_multimodal=True 的模型；无则回退 routing_multimodal。
  - 防幻觉（B.2）：理解结果标 source=ai_vision, confirmed=False —— 默认强制进 clarify 人工确认，
    除非用户勾选「模型自行确认」。本批只负责打标，确认流程在 B批3 的 clarify 增强。
  - 优雅降级：无多模态模型 / 调用失败 → 记 error，不抛异常，不拖垮摄取链路。

复用：models/router.py 的 get_model_by_name + _multimodal_model_from_capabilities。
扫描 PDF 渲染：PyMuPDF（fitz）把页面转 PNG。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 渲染扫描 PDF 时的 DPI（够清晰让模型读字，又不至于图过大）
_PDF_RENDER_DPI = 150
# 扫描 PDF 最多渲染前 N 页（防超大文档拖垮多模态调用/token）
_MAX_PDF_VISION_PAGES = 5
# 多模态理解的输出上限（控制注入草稿的体量）
_VISION_MAX_TOKENS = 1024

# 让模型理解图片/文档图的提示词。强调"如实描述、不要编造"，配合防幻觉打标。
_VISION_PROMPT = """你是需求分析助手。请如实描述这张图片/文档截图里的内容，用于理解软件需求。

要求：
- 客观描述你**实际看到**的：界面元素、文字、流程图、表格、设计稿等。
- 如果是 UI 截图，描述布局、控件、交互暗示。
- 如果是文档/表格，提取其中的文字和结构。
- **不要编造看不清或不存在的内容**；看不清就说"此处不清晰"。
- 输出简洁的中文描述，聚焦对"做什么需求"有用的信息。"""


@dataclass
class VisionResult:
    """单个图片/扫描件的多模态理解结果。"""
    filename: str
    understanding: str = ""        # 模型理解的文本描述
    model_used: str = ""           # 实际使用的多模态模型
    source: str = "ai_vision"      # 固定 ai_vision（防幻觉打标）
    confirmed: bool = False        # 默认 False，待人工确认（B批3 clarify）
    error: str = ""               # 失败原因（非空=失败）

    @property
    def ok(self) -> bool:
        return bool(self.understanding) and not self.error


# ──────────────────────────────────────────────
# 图片 → data URL
# ──────────────────────────────────────────────

_MIME_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _image_to_data_url(path: Path) -> str:
    mime = _MIME_BY_EXT.get(path.suffix.lower(), "image/png")
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _png_bytes_to_data_url(png_bytes: bytes) -> str:
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _render_pdf_pages_to_pngs(path: Path, max_pages: int = _MAX_PDF_VISION_PAGES) -> list[bytes]:
    """把扫描 PDF 的前 max_pages 页渲染成 PNG 字节（PyMuPDF）。"""
    import fitz

    pngs: list[bytes] = []
    with fitz.open(path) as doc:
        n = min(doc.page_count, max_pages)
        for i in range(n):
            page = doc[i]
            pix = page.get_pixmap(dpi=_PDF_RENDER_DPI)
            pngs.append(pix.tobytes("png"))
    return pngs


# ──────────────────────────────────────────────
# 选多模态模型（消费 A 能力库）
# ──────────────────────────────────────────────

def select_vision_model() -> str | None:
    """选一个多模态模型名：能力库优先，回退写死 routing_multimodal。

    返回 None 表示连写死配置都没有（理论上不会，routing_multimodal 有默认值）。
    """
    try:
        from swarm.models.router import ModelRouter

        router = ModelRouter()
        # A.5：优先能力库选出的真·多模态模型
        m = router._multimodal_model_from_capabilities()
        if m:
            return m
        # 回退写死配置
        return router.config.routing_multimodal or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("选多模态模型失败: %s", exc)
        return None


# ──────────────────────────────────────────────
# 多模态理解（单图 / 多图）
# ──────────────────────────────────────────────

def _invoke_vision(model_name: str, data_urls: list[str], prompt: str = _VISION_PROMPT) -> str:
    """调多模态模型理解一张或多张图，返回文本。失败抛异常（由上层捕获）。"""
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    llm = router.get_model_by_name(model_name, temperature=0.1)

    content: list[dict] = [{"type": "text", "text": prompt}]
    for url in data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    resp = llm.invoke([{"role": "user", "content": content}])
    text = resp.content
    if isinstance(text, list):
        # 某些模型返回分段内容
        text = " ".join(
            p if isinstance(p, str) else p.get("text", "")
            for p in text
        )
    return str(text).strip()


def understand_file(
    path: str | Path,
    kind: str,
    *,
    model_name: str | None = None,
) -> VisionResult:
    """理解单个图片或扫描 PDF 文件 → VisionResult（防幻觉打标）。

    kind: "image" | "pdf"（扫描件）。model_name 缺省则自动选多模态模型。
    任何失败都记入 error，不抛异常。
    """
    p = Path(path)
    result = VisionResult(filename=p.name)

    model = model_name or select_vision_model()
    if not model:
        result.error = "无可用多模态模型（请在设置里配置/探测多模态模型）"
        return result
    result.model_used = model

    try:
        if kind == "image":
            data_urls = [_image_to_data_url(p)]
        elif kind == "pdf":
            pngs = _render_pdf_pages_to_pngs(p)
            if not pngs:
                result.error = "扫描 PDF 渲染失败：无页面"
                return result
            data_urls = [_png_bytes_to_data_url(b) for b in pngs]
        else:
            result.error = f"不支持的多模态类型: {kind}"
            return result

        understanding = _invoke_vision(model, data_urls)
        if not understanding:
            result.error = "多模态模型返回空内容"
            return result
        result.understanding = understanding
    except Exception as exc:  # noqa: BLE001
        result.error = f"多模态理解失败: {exc}"
        logger.warning("理解文件 %s 失败: %s", p.name, exc)

    return result


def annotate_for_draft(result: VisionResult) -> str:
    """把理解结果格式化为草稿片段，带防幻觉标注（B.2）。

    明确标注「AI 理解，待确认」，让后续 clarify 知道这段需人工核对。
    """
    if not result.ok:
        return f"【文件: {result.filename}】（多模态理解失败: {result.error}）"
    return (
        f"【文件: {result.filename}】"
        f"（🤖 AI 视觉理解，待人工确认 | 模型: {result.model_used}）\n"
        f"{result.understanding}"
    )
