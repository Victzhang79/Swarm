"""P0 · 技能库 loader：扫描技能目录 → list[SkillDoc]。零代码改动 = 自由拔插低耦合。

**不依赖任何 CLI / Claude Code / MCP**——纯文件解析，可独立运行、可被用户导入。
支持两种 drop-in 形态，同一目录可混放：
  1. **native（本层原生）**：目录下扁平 `*.md`，frontmatter 带 applies_to_*/target/
     priority/max_chars 精确路由。
  2. **imported（第三方）**：`<name>/SKILL.md`（ECC / Claude Code / 用户自有技能包的
     标准布局），frontmatter 只有 name/description。递归发现、零编辑消费。

任一坏文件（无 frontmatter / YAML 损坏 / 无可用 id / 空正文）只【跳过 + warning】，
绝不因一个坏技能拖垮整层注入（fail-open，见 handoff §10.3）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import yaml

from swarm.experience.models import DEFAULT_TARGET, IMPORTED_DEFAULT_PRIORITY, SkillDoc

logger = logging.getLogger(__name__)


def _as_str_tuple(value: object) -> tuple[str, ...]:
    """frontmatter 标量/列表 → 去空去重保序的 tuple[str]。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = str(it).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return tuple(out)


def _as_int(value: object, default: int, *, field: str = "", where: str = "") -> int:
    if value is None:
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # 非法数值静默回退会让作者看不出自己写错了 priority/max_chars（技能照注入但值被忽略）。
        logger.warning(
            "[skills] %s：%s=%r 非法整数，回退默认 %d", where or "<inline>", field, value, default
        )
        return default


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """拆出 `---` 围栏的 YAML frontmatter 与正文。无合法围栏 → None（不猜测，跳过）。"""
    stripped = text.lstrip("﻿")  # 容忍 BOM
    lines = stripped.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return None
    start = idx + 1
    for j in range(start, len(lines)):
        if lines[j].strip() == "---":
            fm = "\n".join(lines[start:j])
            body = "\n".join(lines[j + 1:]).strip("\n")
            return fm, body
    return None


def parse_skill_text(
    text: str, *, source_path: str = "", fallback_id: str = ""
) -> SkillDoc | None:
    """解析单个技能内容 → SkillDoc；不合法返回 None + warning。纯函数，便于单测。

    id 来源优先级：frontmatter `id` → frontmatter `name` → fallback_id（如 `<name>/SKILL.md`
    的父目录名）。三者皆无 → 跳过。native 与 imported 的区别仅在是否显式声明路由字段：
    imported（只有 name/description）→ imported=True 且路由全落宽默认。
    """
    where = source_path or "<inline>"
    split = _split_frontmatter(text)
    if split is None:
        logger.warning("[skills] 跳过 %s：缺少 `---` frontmatter 围栏", where)
        return None
    fm_text, body = split
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        logger.warning("[skills] 跳过 %s：frontmatter YAML 解析失败: %s", where, e)
        return None
    if not isinstance(meta, dict):
        logger.warning("[skills] 跳过 %s：frontmatter 顶层非映射（%s）", where, type(meta).__name__)
        return None

    skill_id = str(meta.get("id") or meta.get("name") or fallback_id or "").strip()
    if not skill_id:
        logger.warning("[skills] 跳过 %s：无可用 id（frontmatter 需含 id 或 name）", where)
        return None
    if not (body or "").strip():
        logger.warning("[skills] 跳过 %s（id=%s）：正文为空", where, skill_id)
        return None

    description = str(meta.get("description") or "").strip()
    title = str(meta.get("title") or meta.get("name") or "").strip() or skill_id

    # 路由字段：native 显式声明则用之；全缺 = imported（宽默认 + 标记）。
    _routing_keys = ("applies_to_stacks", "applies_to_intents", "applies_to_phases", "target")
    has_routing = any(k in meta for k in _routing_keys)
    # G11（阶段E）：imported 收窄——无 description 的第三方 drop-in 是不可判别的全局候选
    # （宽默认+工具 desc 退化），loud 跳过（ECC/Claude Code SKILL.md 规范本就必带
    # description，正常导入零影响）。
    if not has_routing and not description:
        logger.warning(
            "[skills] 跳过 %s（id=%s）：imported 技能缺 description（G11 收窄——"
            "无判别依据的宽默认候选不放行）", where, skill_id)
        return None
    stacks = _as_str_tuple(meta.get("applies_to_stacks")) or ("*",)
    intents = _as_str_tuple(meta.get("applies_to_intents")) or ("*",)
    phases = _as_str_tuple(meta.get("applies_to_phases")) or ("*",)
    target = _as_str_tuple(meta.get("target")) or DEFAULT_TARGET

    return SkillDoc(
        id=skill_id,
        title=title,
        body=body,
        target=target,
        applies_to_stacks=stacks,
        applies_to_intents=intents,
        applies_to_phases=phases,
        # G11：imported 默认 priority 低于 native 默认 50——宽匹配不该再占高位
        priority=_as_int(meta.get("priority"),
                         (50 if has_routing else IMPORTED_DEFAULT_PRIORITY),
                         field="priority", where=where),
        max_chars=_as_int(meta.get("max_chars"), 1200, field="max_chars", where=where),
        summary=description,
        tags=_as_str_tuple(meta.get("tags")),
        # E9-11（复核 RF10）：拔插开关 fail-closed——只认显式启用值；带引号的 "off"/
        # "disabled" 等未知值一律按 disabled（旧黑名单会把手滑加引号的下架件悄悄放回）。
        enabled=(True if "enabled" not in meta
                 else str(meta.get("enabled")).strip().lower() in ("true", "1", "yes", "on")),
        source_path=source_path,
        imported=not has_routing,
    )


# 技能目录里合法但非技能的文档文件（静默跳过，不刷 warning）。
_SKIP_FLAT_NAMES = {"readme.md", "changelog.md", "license.md", "contributing.md", "index.md"}


def _discover_paths(root: Path) -> list[tuple[Path, str]]:
    """发现技能文件 → [(path, fallback_id)]。

    - 扁平 `*.md`（不含 SKILL.md 与常见文档文件）：native 布局，fallback_id=文件名 stem。
    - 递归 `**/SKILL.md`：第三方按目录组织的布局，fallback_id=父目录名。
    去重：同一 path 只收一次。稳定排序（按 path 字符串）保证确定性。
    """
    found: dict[Path, str] = {}
    for path in root.glob("*.md"):
        if path.name == "SKILL.md" or path.name.lower() in _SKIP_FLAT_NAMES:
            continue
        found.setdefault(path, path.stem)
    for path in root.rglob("SKILL.md"):
        parent = path.parent.name or path.stem
        found.setdefault(path, parent)
    return sorted(found.items(), key=lambda kv: str(kv[0]))


def load_skills(directory: str | Path) -> list[SkillDoc]:
    """扫描单个目录 → SkillDoc 列表（按 id 稳定排序）。

    目录不存在/为空 → []（fail-open，不报错）。坏文件跳过 + warning。
    重复 id → 保留先出现者（按 path 稳定序），后者跳过 + warning。
    """
    root = Path(directory)
    if not root.is_dir():
        logger.debug("[skills] 技能库目录不存在或非目录：%s（返回空）", root)
        return []
    docs: list[SkillDoc] = []
    seen_ids: set[str] = set()
    for path, fallback_id in _discover_paths(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # UnicodeDecodeError(ValueError 子类)不属 OSError——不接住会窜出本循环，
            # 让 load_skills_from 的目录级 except 吞掉【整个目录】，同目录好技能也一并丢
            # （违背"per-file 隔离"承诺）。窄接住 → 只跳过坏字节的这一个文件。
            logger.warning("[skills] 跳过 %s：读取/解码失败 %s", path, e)
            continue
        doc = parse_skill_text(text, source_path=str(path), fallback_id=fallback_id)
        if doc is None:
            continue
        if doc.id in seen_ids:
            logger.warning("[skills] 跳过 %s：技能 id '%s' 重复（保留先出现者）", path, doc.id)
            continue
        seen_ids.add(doc.id)
        docs.append(doc)
    docs.sort(key=lambda d: d.id)
    return docs


def load_skills_from(dirs: Iterable[str | Path]) -> list[SkillDoc]:
    """合并多个技能目录（内置库 + 用户导入目录）。

    先出现的目录优先：跨目录 id 冲突时保留先加载者（用户可用靠前目录覆盖内置技能）。
    任一目录异常只跳过该目录 + warning，其余照常（fail-open）。
    """
    merged: list[SkillDoc] = []
    seen_ids: set[str] = set()
    for d in dirs:
        try:
            docs = load_skills(d)
        except Exception as e:  # noqa: BLE001 — 单目录异常不拖垮整体
            logger.warning("[skills] 加载目录 %s 失败，跳过：%s", d, e)
            continue
        for doc in docs:
            if doc.id in seen_ids:
                logger.debug("[skills] 跨目录 id '%s' 冲突，保留先加载者", doc.id)
                continue
            seen_ids.add(doc.id)
            merged.append(doc)
    merged.sort(key=lambda d: d.id)
    return merged
