"""黄金集 — 从真实 L2(mem_task_summary)派生，PG 无数据时回退合成集。

派生规则(WS0，id 直链优先，干净无歧义):
  每条 L2 摘要 → 一个召回样本。
    query        = summary（一句话摘要，正是写入时的语义锚点）
    relevant_ids = 该 L2 metadata 回写的 success_id / mistake_id（learn_store.py:133/204）
    relevant_modules = metadata.modules（module/tag 重叠扩召回留作开放小项，WS0 不启用）
  只有带直链 id 的 L2 才进召回集（无链的摘要无法判命中）。

合成集(--synthetic)：自带成对结构，专门暴露"近因/遗忘"问题——
  - 每个主题一条【新鲜相关】+ 一条【陈旧近义】（措辞相近、应被压下）
  - 若干不相关干扰项
合成集是纯内存 spec（不依赖 DB），既供 harness 播种，也供单测直接校验指标逻辑。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class GoldenSample:
    id: str
    query: str
    relevant_ids: list[Any] = field(default_factory=list)
    relevant_modules: list[str] = field(default_factory=list)
    kind: str = "l5"            # l5 / l6
    created_at: str | None = None
    source: str = "l2"          # l2 / synthetic


# ──────────────────────────────────────────────
# 真实 L2 派生
# ──────────────────────────────────────────────

async def derive_golden_from_l2(
    store: Any, project_id: str, limit: int = 50
) -> list[GoldenSample]:
    """从 mem_task_summary 派生召回样本（id 直链）。store 为已连接的 MemoryStore。"""
    summaries = await store.query_task_summaries(project_id, limit=limit)
    samples: list[GoldenSample] = []
    for i, s in enumerate(summaries):
        meta = s.get("metadata") or {}
        sid = meta.get("success_id")
        mid = meta.get("mistake_id")
        query = (s.get("summary") or "").strip()
        if not query:
            continue
        created = s.get("created_at")
        created_str = created.isoformat() if hasattr(created, "isoformat") else (
            str(created) if created else None
        )
        if sid is not None:
            samples.append(GoldenSample(
                id=f"l6-{i}", query=query, relevant_ids=[sid],
                relevant_modules=list(meta.get("modules") or []),
                kind="l6", created_at=created_str, source="l2",
            ))
        if mid is not None:
            samples.append(GoldenSample(
                id=f"l5-{i}", query=query, relevant_ids=[mid],
                relevant_modules=list(meta.get("modules") or []),
                kind="l5", created_at=created_str, source="l2",
            ))
    return samples


# ──────────────────────────────────────────────
# 合成集（纯内存 spec）
# ──────────────────────────────────────────────

@dataclass
class SyntheticEntry:
    """合成记忆条目 spec。harness 据此播种 store；is_stale 决定是否额外老化。"""
    local_id: str               # 播种后映射到真实 DB id
    kind: str                   # l5 / l6
    error_type: str             # l5 用；l6 忽略
    text: str                   # 描述（embed 锚点）
    is_stale: bool              # 是否"陈旧应遗忘"
    theme: str                  # 主题分组键


def synthetic_catalog() -> list[SyntheticEntry]:
    """3 个主题，每主题 1 新鲜 + 1 陈旧近义；外加 2 个干扰项。"""
    cat: list[SyntheticEntry] = []
    themes = [
        ("sort", "compile_error",
         "用户列表排序时 Mapper.xml 缺少动态 ORDER BY 的 if 判断导致 sortField 为空 SQL 报错",
         "列表排序未在 XML 加 <if> 动态拼接 ORDER BY，空排序字段时 SQL 语法错误"),
        ("auth", "logic_error",
         "登录接口未对 token 过期做兜底，过期后返回 500 而非 401 跳转",
         "鉴权 token 失效时缺少 401 兜底处理，直接抛 500"),
        ("page", "integration_failure",
         "分页查询未用 PageResult 包装，前端拿不到 total 字段",
         "新增分页接口忘记 PageResult 统一包装，缺 total 导致前端分页失效"),
    ]
    for theme, etype, fresh, stale in themes:
        cat.append(SyntheticEntry(f"{theme}-fresh", "l5", etype, fresh, False, theme))
        cat.append(SyntheticEntry(f"{theme}-stale", "l5", etype, stale, True, theme))
    cat.append(SyntheticEntry("noise-1", "l5", "style_violation",
                              "变量命名不符合驼峰规范 checkstyle 告警", True, "noise"))
    cat.append(SyntheticEntry("noise-2", "l5", "logic_error",
                              "缓存击穿未加互斥锁导致数据库瞬时压力", True, "noise"))
    return cat


# 中性探针 query：与 fresh/stale 都相关但都不雷同 —— 故意不送分，
# 让纯余弦无法靠"精确匹配"区分 fresh/stale，近因排序才有判别力。
THEME_PROBES: dict[str, str] = {
    "sort": "列表排序的动态 SQL 怎么处理空排序字段避免报错",
    "auth": "登录鉴权里 token 过期/失效应该如何兜底返回",
    "page": "分页接口如何统一返回 total 给前端",
}


def synthetic_probes() -> dict[str, str]:
    return dict(THEME_PROBES)


def synthetic_samples() -> list[GoldenSample]:
    """合成召回样本：用中性 probe 查询；relevant=该主题 fresh+stale 两条(都是合法召回)。

    召回分只衡量"该主题是否被召回"(fresh/stale 任一在 top-k 即算)，
    把"fresh 是否排在 stale 之前"的判别留给独立的近因指标，互不污染。
    """
    out: list[GoldenSample] = []
    for theme, probe in THEME_PROBES.items():
        out.append(GoldenSample(
            id=f"syn-{theme}", query=probe,
            relevant_ids=[f"{theme}-fresh", f"{theme}-stale"],
            kind="l5", source="synthetic",
        ))
    return out


# ──────────────────────────────────────────────
# jsonl 读写（对齐 retrieval_bench 约定）
# ──────────────────────────────────────────────

def save_samples(samples: list[GoldenSample], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for s in samples:
            fh.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")


def load_samples(path: str) -> list[GoldenSample]:
    out: list[GoldenSample] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(GoldenSample(**json.loads(line)))
    return out
