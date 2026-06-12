"""Layer C 增强 — 从【实际代码】推断项目工程惯例（"资深工程师读代码"）。

config 文件提取（norms_extractor）很浅：老项目（如 RuoYi）没有 .editorconfig/
.ruff.toml，规范命中=0。但一个熟悉项目的工程师不靠配置文件，而是【读几份代表性
源码】就能总结出：命名风格、分层约定、错误处理惯例、有哪些现成工具类该复用、测试
怎么组织。本模块用 LLM 做这件事，产出 tag='inferred' 的 Norm，喂给 worker。

设计：
- 选代表性文件（每语言挑若干非测试、体量适中的源文件）
- 拼进 prompt，让本地模型按固定 JSON schema 输出 norms
- 解析为 Norm 列表（tag='inferred'，priority 适中，低于人工/config）
失败静默返回 []（不阻断预处理）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from swarm.config.settings import ModelConfig
from swarm.knowledge.norms_store import Norm

logger = logging.getLogger(__name__)

# 每语言取样文件数 / 单文件最大字符 / 总预算
_MAX_FILES_PER_LANG = 3
_MAX_CHARS_PER_FILE = 4000
_MAX_TOTAL_CHARS = 24000

_SOURCE_EXT = {
    "python": [".py"],
    "java": [".java"],
    "javascript": [".js", ".jsx", ".ts", ".tsx", ".vue"],
    "go": [".go"],
    "rust": [".rs"],
}

_EXCLUDE_DIR = {
    ".git", ".venv", "venv", "node_modules", "target", "build", "dist",
    "__pycache__", ".idea", ".codegraph", "test", "tests", "__tests__",
}


def _is_excluded(p: Path) -> bool:
    return any(seg in _EXCLUDE_DIR for seg in p.parts)


def _pick_sample_files(project_path: str) -> list[Path]:
    """挑代表性源文件：每语言取体量适中(非最大非最小)的若干个非测试文件。

    体量适中的更可能是"典型业务实现"，能体现惯例；过大多为生成/聚合，过小无信息。
    """
    root = Path(project_path)
    if not root.is_dir():
        return []
    by_lang: dict[str, list[tuple[int, Path]]] = {}
    for lang, exts in _SOURCE_EXT.items():
        for ext in exts:
            for fp in root.rglob(f"*{ext}"):
                if _is_excluded(fp) or not fp.is_file():
                    continue
                name = fp.name.lower()
                if "test" in name or name.startswith("_") or name == "__init__.py":
                    continue
                try:
                    size = fp.stat().st_size
                except OSError:
                    continue
                if size < 200 or size > 40000:  # 过滤过小/过大
                    continue
                by_lang.setdefault(lang, []).append((size, fp))
    picks: list[Path] = []
    for lang, items in by_lang.items():
        items.sort(key=lambda x: x[0])
        # 取中位数附近的文件（典型实现）
        if not items:
            continue
        mid = len(items) // 2
        window = items[max(0, mid - 1): mid - 1 + _MAX_FILES_PER_LANG]
        picks.extend(fp for _, fp in window)
    return picks


def _read_samples(files: list[Path], root: Path) -> str:
    """读取样本文件，拼成带相对路径标注的代码块，受总预算约束。"""
    parts: list[str] = []
    total = 0
    for fp in files:
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        text = text[:_MAX_CHARS_PER_FILE]
        try:
            rel = fp.relative_to(root).as_posix()
        except ValueError:
            rel = fp.name
        block = f"### {rel}\n```\n{text}\n```\n"
        if total + len(block) > _MAX_TOTAL_CHARS:
            break
        parts.append(block)
        total += len(block)
    return "\n".join(parts)


_PROMPT_SYSTEM = (
    "你是资深工程师，正在快速熟悉一个陌生项目以便接手开发。"
    "你会通过阅读几份代表性源码，总结这个项目【实际遵循】的工程惯例，"
    "供后续开发者严格遵循，避免另起炉灶、风格割裂。只总结代码里真实体现的，不要臆造。"
)

_PROMPT_USER_TMPL = """下面是项目 {name} 的若干代表性源码。请总结该项目的实际工程惯例。

{samples}

请只输出一个 JSON 数组，每个元素形如：
{{"title": "简短标题", "tag": "naming|architecture|error_handling|testing|utility|general", "content": "一句话可执行的约定（含具体例子/类名/方法名）", "priority": 1-5}}

要求：
- 8~15 条，覆盖：命名风格、分层/包结构约定、错误与异常处理方式、日志方式、
  现成可复用的工具类/基类（写出确切类名，提醒优先复用）、测试组织方式（如有）。
- content 要具体到能直接照做（坏例："遵循良好命名"；好例："工具类统一放 com.x.common.utils 下，全为 static 方法，如 StringUtils.isBlank，新增工具方法应加到对应工具类而非新建"）。
- 只输出 JSON 数组，不要任何解释文字。
- 直接给出 JSON，不要输出思考过程/<think> 标签。
- JSON 字段名严格小写：title / tag / content / priority。

/no_think"""


def _parse_norms_json(raw: str) -> list[Norm]:
    """从 LLM 输出里抽 JSON 数组并转 Norm 列表。容错：剥 code fence、找首个 [。"""
    if not raw:
        return []
    text = raw.strip()
    # 剥掉推理模型的 <think>...</think> 块（MiniMax/Qwen 等会先输出思考再给 JSON）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # 未闭合的 <think>（被 max_tokens 截断）：取最后一个 </think> 之后，或丢弃 think 起始
    if "<think>" in text.lower():
        idx = text.lower().rfind("</think>")
        if idx != -1:
            text = text[idx + len("</think>"):]
        else:
            text = text[: text.lower().find("<think>")]
    text = text.strip()
    # 剥 ```json ... ```
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # 截取首个 JSON 数组
    start = text.find("[")
    end = text.rfind("]")
    arr = None
    if start != -1 and end != -1 and end > start:
        try:
            arr = json.loads(text[start: end + 1])
        except Exception:
            arr = None
    # 数组整体解析失败（截断/末尾多逗号等）→ 逐个对象 salvage：正则抠出每个 {...} 单独 parse
    if not isinstance(arr, list):
        arr = []
        for obj_str in re.findall(r"\{[^{}]*\}", text, re.DOTALL):
            try:
                obj = json.loads(obj_str)
                if isinstance(obj, dict):
                    arr.append(obj)
            except Exception:
                continue
    if not arr:
        return []
    norms: list[Norm] = []
    valid_tags = {"naming", "architecture", "error_handling", "testing", "utility", "general"}
    for item in arr:
        if not isinstance(item, dict):
            continue
        # 大小写不敏感取键（模型有时输出 "Title"/"Content" 等）
        low = {str(k).lower(): v for k, v in item.items()}
        title = str(low.get("title") or low.get("name") or "").strip()
        content = str(low.get("content") or low.get("description") or low.get("desc") or "").strip()
        if not title or not content:
            continue
        tag = str(low.get("tag") or low.get("category") or "general").strip().lower()
        if tag not in valid_tags:
            tag = "general"
        try:
            priority = int(low.get("priority", 2))
        except (TypeError, ValueError):
            priority = 2
        priority = max(0, min(priority, 5))
        norms.append(Norm(
            title=title[:200],
            content=content[:800],
            tag=tag,
            priority=priority,
            metadata={"source": "inferred"},
        ))
    return norms


def _call_llm(project_name: str, samples: str) -> str:
    """调本地模型推断惯例。OpenAI 兼容；失败回退 SiliconFlow；都失败返回空。"""
    cfg = ModelConfig()
    user = _PROMPT_USER_TMPL.format(name=project_name, samples=samples)
    messages = [
        {"role": "system", "content": _PROMPT_SYSTEM},
        {"role": "user", "content": user},
    ]
    # 本地优先
    for base_url, api_key, model in (
        (cfg.local_base_url, cfg.local_api_key or "dummy", cfg.routing_medium or "MiniMax-M2.7-Pro"),
        (cfg.siliconflow_base_url, cfg.siliconflow_api_key, cfg.brain_primary),
    ):
        if not base_url:
            continue
        try:
            from openai import OpenAI
            client = OpenAI(base_url=base_url, api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=4000,
            )
            content = resp.choices[0].message.content or ""
            if content.strip():
                return content
        except Exception as exc:  # noqa: BLE001
            logger.warning("norms 推断 LLM 调用失败(%s): %s", base_url, exc)
    return ""


def infer_norms_from_code(project_path: str, project_name: str = "") -> list[Norm]:
    """主入口：读代表性源码 → LLM 推断 → 解析为 Norm 列表（tag='inferred'）。

    纯函数、不写库；失败返回 []。供 preprocess Phase 1.6 调用。
    """
    root = Path(project_path)
    files = _pick_sample_files(project_path)
    if not files:
        logger.info("norms 推断：未找到代表性源文件 %s", project_path)
        return []
    samples = _read_samples(files, root)
    if not samples.strip():
        return []
    raw = _call_llm(project_name or root.name, samples)
    norms = _parse_norms_json(raw)
    logger.info("norms 推断：样本 %d 文件 → 推断出 %d 条惯例", len(files), len(norms))
    return norms
