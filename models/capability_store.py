"""模型能力库 — ModelCapability 表 + CRUD + 启发式默认（设计 v3 A 部分基础设施）

单一真相源：每个 (provider_id, model_id) 一条能力记录，记录真实
context_window / 多模态支持 / 生成速度 / 来源。路由、上下文预算、多模态选型
全部读这张表，消除散落各处的写死常量（routing_multimodal / context_max_tokens 等）。

设计取舍（见 docs/Multimodal_Ingestion_Design.md A.3）：
  - 独立表（非塞进 config JSON）—— 可重探、有 probed_at 时间戳、可审计。
  - source ∈ {probed, parsed, manual, default} 区分数据来源，让 UI 能标注
    "已探测 / 错误消息解析 / 人工修正 / 默认兜底（未探明）"。

本批（A批1）只做：表 + CRUD + 启发式默认 + 单测。真实探测在 A批2。
风格与 swarm/project/store.py 一致（psycopg 同步 + 模块级函数 + conn_str 兜底）。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import psycopg

from swarm.config.settings import DatabaseConfig

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 合法 source 取值（与设计 A.3 对齐）
# ──────────────────────────────────────────────
SOURCE_PROBED = "probed"    # 真实 API 探测拿到（最可信）
SOURCE_PARSED = "parsed"    # 从错误消息 / models 字段解析（A.2.1 第 1-3 层）
SOURCE_MANUAL = "manual"    # 用户人工修正
SOURCE_DEFAULT = "default"  # 启发式兜底（未探明，UI 需提示）
VALID_SOURCES = frozenset({SOURCE_PROBED, SOURCE_PARSED, SOURCE_MANUAL, SOURCE_DEFAULT})

# ──────────────────────────────────────────────
# PG DDL
# ──────────────────────────────────────────────

MODEL_CAPABILITIES_DDL = """
CREATE TABLE IF NOT EXISTS model_capabilities (
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    context_window INTEGER,
    supports_multimodal BOOLEAN NOT NULL DEFAULT FALSE,
    gen_speed_tps REAL NOT NULL DEFAULT 0.0,
    kind TEXT NOT NULL DEFAULT 'cloud',
    source TEXT NOT NULL DEFAULT 'default',
    note TEXT NOT NULL DEFAULT '',
    probed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (provider_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_model_cap_provider ON model_capabilities(provider_id);
"""

ALL_DDL = [MODEL_CAPABILITIES_DDL]

_CAP_SELECT = """
    provider_id, model_id, context_window, supports_multimodal,
    gen_speed_tps, kind, source, note, probed_at, created_at, updated_at
"""


# ──────────────────────────────────────────────
# 启发式默认（A.2.1 第 4 层 —— 探测失败 / 未探测时的保守兜底）
# ──────────────────────────────────────────────

# 本地小模型保守给 32k，大模型 128k（设计 A.2.1 第 4 步）。
# 命中名字里的规模/系列线索时调整。绝不假装精确，全部标 source=default。
_DEFAULT_LOCAL_SMALL = 32_000
_DEFAULT_LOCAL_LARGE = 128_000
_DEFAULT_CLOUD = 128_000

# 模型名 → 已知近似 context_window 的线索（保守取值，仍标 default）。
# 仅作启发式排序参考，真值以探测为准。键为小写子串。
_CONTEXT_HINTS: list[tuple[str, int]] = [
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4", 8_000),
    ("gpt-3.5", 16_000),
    ("claude-3", 200_000),
    ("claude-4", 200_000),
    ("kimi", 128_000),
    ("moonshot", 128_000),
    ("glm-4", 128_000),
    ("glm-5", 128_000),
    # 122B-A10B 本地部署实测 max_model_len=65536（2026-07-12 网关元数据），
    # 必须先于 "qwen3" 泛匹配命中，否则高估一倍 → 上下文预算超包 400。
    ("122b-a10b", 64_000),
    # ThinkingCap-Qwen3.6-27B(2026-07-15 换装 27B-Saka)标称 256K，须先于 "qwen3" 泛匹配
    # (名字含 qwen3.6)命中，否则被低估成 128K。真值仍以探测为准，此为无探测时的兜底。
    ("thinkingcap", 256_000),
    ("qwen3", 128_000),
    ("deepseek", 64_000),
    ("minimax", 200_000),
]

# 名字里出现这些子串时，倾向判断为多模态（仅启发式默认；真值靠探测）。
# "thinkingcap"：ThinkingCap-Qwen3.6-27B 含视觉能力(2026-07-15 用户确认，等效原 Saka-mm)，
# 但名字无 vl/vision 线索 → 显式登记，否则多模态路由把它当纯文本、图像子任务无本地承接。
_MULTIMODAL_HINTS = ("vl", "vision", "multimodal", "-mm", "omni", "gpt-4o", "step-3", "thinkingcap")


def _normalize_size_token(model_id: str) -> int | None:
    """从模型名抽参数规模（如 27B / 122B / 7b）→ 粗判大小模型。

    返回参数量（单位：十亿 B）；解析不到返回 None。
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_id.lower())
    if m:
        try:
            return int(float(m.group(1)))
        except ValueError:
            return None
    return None


def heuristic_context_window(model_id: str, kind: str = "cloud") -> int:
    """启发式 context_window 默认值（A.2.1 第 4 层兜底）。

    保守优先：先看明确的参数规模标识（如 7b），小模型（<20B）压到 32k——
    本地小模型常受显存限制，标称大窗口未必开满，保守兜底等真实探测。
    无规模标识时查名字线索表（系列标称值），再退到 kind 默认。绝不假装精确。
    """
    name = (model_id or "").lower()

    # 1) 明确小规模标识优先（保守）：本地小模型压到 32k
    size_b = _normalize_size_token(name)
    if kind == "local" and size_b is not None and size_b < 20:
        return _DEFAULT_LOCAL_SMALL

    # 2) 名字线索表（系列标称窗口）
    for needle, window in _CONTEXT_HINTS:
        if needle in name:
            return window

    # 3) kind 默认兜底
    if kind == "local":
        return _DEFAULT_LOCAL_LARGE
    return _DEFAULT_CLOUD


def heuristic_supports_multimodal(model_id: str) -> bool:
    """启发式判断是否多模态（仅默认兜底；真值靠 A批2 发带图小请求探测）。"""
    name = (model_id or "").lower()
    return any(h in name for h in _MULTIMODAL_HINTS)


def default_capability(provider_id: str, model_id: str, kind: str = "cloud") -> dict[str, Any]:
    """构造一条启发式默认能力记录（source=default，待探测/人工确认）。"""
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "context_window": heuristic_context_window(model_id, kind),
        "supports_multimodal": heuristic_supports_multimodal(model_id),
        "gen_speed_tps": 0.0,
        "kind": kind,
        "source": SOURCE_DEFAULT,
        "note": "启发式默认，未探测",
        "probed_at": None,
    }


# ──────────────────────────────────────────────
# 连接辅助（与 project/store.py 一致）
# ──────────────────────────────────────────────

def _get_conn_str(db_config: DatabaseConfig | None = None) -> str:
    """获取 PG 连接字符串（§3.2：委托 infra.db 单一来源，本地名保 seam）"""
    from swarm.infra.db import pg_conn_str
    return pg_conn_str(db_config)


def ensure_tables(conn_str: str | None = None) -> None:
    """同步建表（幂等）。由 scripts/init_db.py 与 app on_startup 统一调用。"""
    conn_str = conn_str or _get_conn_str()
    from swarm.infra.db import pg_connect_timeout_kwargs

    # D15：直连补 connect_timeout——PG 黑洞时启动建表有界快失败，不无限挂。
    with psycopg.connect(conn_str, autocommit=True, **pg_connect_timeout_kwargs()) as conn:
        with conn.cursor() as cur:
            for ddl in ALL_DDL:
                cur.execute(ddl)
    logger.info("model_capabilities table ensured")


def _get_conn(conn_str: str | None = None):
    """池化连接上下文管理器（autocommit），与 project/store 一致。"""
    from swarm.infra.db import sync_pool

    return sync_pool(conn_str).connection()


# ──────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────

def upsert_capability(
    provider_id: str,
    model_id: str,
    *,
    context_window: int | None = None,
    supports_multimodal: bool = False,
    gen_speed_tps: float = 0.0,
    kind: str = "cloud",
    source: str = SOURCE_DEFAULT,
    note: str = "",
    probed_at: datetime | None = None,
    conn_str: str | None = None,
) -> dict[str, Any]:
    """插入或更新一条能力记录（按 provider_id + model_id 主键 upsert）。

    source 非法时回退 default 并告警 —— 防止脏值污染来源审计。
    """
    if source not in VALID_SOURCES:
        logger.warning("非法 source=%r，回退 default", source)
        source = SOURCE_DEFAULT

    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO model_capabilities (
                    provider_id, model_id, context_window, supports_multimodal,
                    gen_speed_tps, kind, source, note, probed_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (provider_id, model_id) DO UPDATE SET
                    context_window = EXCLUDED.context_window,
                    supports_multimodal = EXCLUDED.supports_multimodal,
                    gen_speed_tps = EXCLUDED.gen_speed_tps,
                    kind = EXCLUDED.kind,
                    source = EXCLUDED.source,
                    note = EXCLUDED.note,
                    probed_at = EXCLUDED.probed_at,
                    updated_at = NOW()
                RETURNING {_CAP_SELECT}
                """,
                (
                    provider_id, model_id, context_window, supports_multimodal,
                    gen_speed_tps, kind, source, note, probed_at,
                ),
            )
            row = cur.fetchone()
    return _row_to_capability(row)


def get_capability(
    provider_id: str, model_id: str, conn_str: str | None = None
) -> dict[str, Any] | None:
    """读单条能力记录；不存在返回 None。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_CAP_SELECT} FROM model_capabilities "
                "WHERE provider_id = %s AND model_id = %s",
                (provider_id, model_id),
            )
            row = cur.fetchone()
    return _row_to_capability(row) if row else None


def list_capabilities(
    provider_id: str | None = None, conn_str: str | None = None
) -> list[dict[str, Any]]:
    """列出能力记录；provider_id 给定则过滤。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            if provider_id:
                cur.execute(
                    f"SELECT {_CAP_SELECT} FROM model_capabilities "
                    "WHERE provider_id = %s ORDER BY model_id",
                    (provider_id,),
                )
            else:
                cur.execute(
                    f"SELECT {_CAP_SELECT} FROM model_capabilities "
                    "ORDER BY provider_id, model_id"
                )
            rows = cur.fetchall()
    return [_row_to_capability(r) for r in rows]


def delete_capability(
    provider_id: str, model_id: str, conn_str: str | None = None
) -> bool:
    """删一条能力记录；删到返回 True。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM model_capabilities "
                "WHERE provider_id = %s AND model_id = %s",
                (provider_id, model_id),
            )
            return cur.rowcount > 0


def delete_provider_capabilities(provider_id: str, conn_str: str | None = None) -> int:
    """删某 provider 全部能力记录（重探前清场用）；返回删除行数。"""
    with _get_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM model_capabilities WHERE provider_id = %s",
                (provider_id,),
            )
            return cur.rowcount


def get_capability_or_default(
    provider_id: str,
    model_id: str,
    kind: str = "cloud",
    conn_str: str | None = None,
) -> dict[str, Any]:
    """读能力记录，缺失则返回启发式默认（不落库）。

    消费方（上下文预算 / 多模态选型）的统一读取入口：永远拿得到一条记录，
    缺失时是 source=default 的兜底，调用方据此决定是否提示用户重探。
    """
    cap = get_capability(provider_id, model_id, conn_str=conn_str)
    if cap is not None:
        return cap
    return default_capability(provider_id, model_id, kind)


# ──────────────────────────────────────────────
# row → dict
# ──────────────────────────────────────────────

def _row_to_capability(row: tuple | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "provider_id": row[0],
        "model_id": row[1],
        "context_window": row[2],
        "supports_multimodal": row[3],
        "gen_speed_tps": row[4],
        "kind": row[5],
        "source": row[6],
        "note": row[7],
        "probed_at": row[8].isoformat() if row[8] else None,
        "created_at": row[9].isoformat() if row[9] else None,
        "updated_at": row[10].isoformat() if row[10] else None,
    }
