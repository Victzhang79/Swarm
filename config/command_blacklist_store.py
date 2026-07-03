"""命令安全黑名单存储 — A2 批3（落库 + 管理员可配 + 内置默认）。

设计（符合"配置落库 + WebUI 可配 + 保存即生效"）：
- 规则落 PG 表 command_blacklist（非 .env 写死）。
- 内置默认危险规则（首次建表时 seed）：防误操作（rm -rf /、fork bomb、dd 覆盖设备等）。
- run_command 执行前匹配；命中拒绝 + 审计留痕。
- 诚实定位：防误操作，非防恶意（恶意由 CubeSandbox 沙箱层隔离兜底，见 A2 实测）。

匹配语义：每条规则是一个正则 pattern（对整条命令做 search）。enabled 控制启停。
"""

from __future__ import annotations

import logging
import re
import threading
import time

import psycopg

from swarm.config.settings import DatabaseConfig
from swarm.infra.db import sync_pool

logger = logging.getLogger(__name__)

COMMAND_BLACKLIST_DDL = """
CREATE TABLE IF NOT EXISTS command_blacklist (
    id          SERIAL PRIMARY KEY,
    pattern     TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    enabled     BOOLEAN NOT NULL DEFAULT true,
    builtin     BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ DEFAULT now()
);
"""

# 内置默认规则（防误操作）。pattern 为正则，对整条命令 search。
_DEFAULT_RULES: list[tuple[str, str]] = [
    (r"\brm\s+-rf?\s+(/|/\*|~|\$HOME)(\s|$)", "递归删除根/家目录"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bdd\b.*\bof=/dev/(sd|nvme|disk|hd)", "dd 覆盖块设备"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r">\s*/dev/(sd|nvme|disk|hd)", "重定向写块设备"),
    (r"\bchmod\s+-R\s+777\s+/(\s|$)", "递归 777 根目录"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "关机/重启宿主"),
    (r"\bmv\s+/\s+", "移动根目录"),
]

_CACHE: list | None = None
_CACHE_AT = 0.0
_CACHE_TTL = 30.0
# P2：缓存有并发读写（多 worker 线程同时 run_command），加锁防竞态读到半更新状态。
_CACHE_LOCK = threading.Lock()


def _compile_default_rules() -> list[tuple[int, str, str]]:
    """把内置默认危险规则编成 (id, pattern, desc)，作为 DB 不可用时的安全基线。"""
    out: list[tuple[int, str, str]] = []
    for i, (pat, desc) in enumerate(_DEFAULT_RULES):
        try:
            re.compile(pat)
            out.append((-(i + 1), pat, desc))  # 负 id 标记内置兜底
        except re.error:
            continue
    return out


def _conn_str() -> str:
    return DatabaseConfig().postgres_uri


def ensure_tables(conn_str: str | None = None) -> None:
    """建 command_blacklist 表（幂等）+ seed 内置默认规则。由 init_db/startup 调用。"""
    conn_str = conn_str or _conn_str()
    with psycopg.connect(conn_str, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(COMMAND_BLACKLIST_DDL)
            # seed 内置规则（仅当表内无 builtin 规则时，避免重复 seed / 覆盖用户删除）
            cur.execute("SELECT COUNT(*) FROM command_blacklist WHERE builtin = true")
            row = cur.fetchone()
            if row and row[0] == 0:
                for pat, desc in _DEFAULT_RULES:
                    cur.execute(
                        "INSERT INTO command_blacklist (pattern, description, enabled, builtin) "
                        "VALUES (%s, %s, true, true)",
                        (pat, desc),
                    )
                logger.info("command_blacklist seeded %d builtin rules", len(_DEFAULT_RULES))
    logger.info("command_blacklist table ensured")


def _pooled_conn(conn_str: str | None = None):
    return sync_pool(conn_str or _conn_str()).connection()


def list_rules(conn_str: str | None = None) -> list[dict]:
    """全部规则（管理用，含 disabled）。db 错误返回空。"""
    try:
        with _pooled_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, pattern, description, enabled, builtin FROM command_blacklist ORDER BY id"
                )
                rows = cur.fetchall()
        return [
            {"id": r[0], "pattern": r[1], "description": r[2], "enabled": r[3], "builtin": r[4]}
            for r in rows
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_rules failed: %s", exc)
        return []


def _enabled_patterns(conn_str: str | None = None) -> list[tuple[int, str, str]]:
    """启用的规则 (id, pattern, description)，带 TTL 缓存（加锁）。

    P2 加固：① 缓存读写加锁防并发竞态；② DB 错误不再【完全 fail-open 放行】——优先复用
    上次成功加载的缓存（即便已过 TTL，旧规则胜过无规则）；连缓存都没有时退回【内置默认
    危险规则基线】(_compile_default_rules)，保证 rm -rf / / fork bomb 等始终被拦，而非 DB
    一抖就全放行。
    """
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    with _CACHE_LOCK:
        if _CACHE is not None and (now - _CACHE_AT) < _CACHE_TTL:
            return list(_CACHE)
    try:
        with _pooled_conn(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, pattern, description FROM command_blacklist WHERE enabled = true"
                )
                rows = cur.fetchall()
        compiled = []
        for rid, pat, desc in rows:
            try:
                re.compile(pat)
                compiled.append((rid, pat, desc))
            except re.error:
                logger.warning("command_blacklist 规则 %d 正则无效，跳过: %s", rid, pat)
        with _CACHE_LOCK:
            _CACHE = compiled
            _CACHE_AT = now
        return list(compiled)
    except Exception as exc:  # noqa: BLE001
        with _CACHE_LOCK:
            stale = list(_CACHE) if _CACHE is not None else None
        if stale is not None:
            logger.warning("加载命令黑名单失败，复用上次缓存(%d 条)以免失保护: %s", len(stale), exc)
            return stale
        baseline = _compile_default_rules()
        logger.warning(
            "加载命令黑名单失败且无缓存，退回内置默认基线(%d 条，非完全放行): %s",
            len(baseline), exc,
        )
        return baseline


def invalidate_cache() -> None:
    global _CACHE, _CACHE_AT
    with _CACHE_LOCK:
        _CACHE = None
        _CACHE_AT = 0.0


def check_command(command: str, conn_str: str | None = None) -> tuple[bool, str]:
    """检查命令是否命中黑名单。

    返回 (allowed, reason)：allowed=False 时 reason 为命中的规则描述。
    db 不可用时 fail-open（放行）——黑名单是防误操作的便利层，不应因 db 故障阻断业务；
    真正的安全边界是 CubeSandbox 沙箱隔离（已实测：非 root/网络封锁/资源限额）。
    """
    if not command:
        return True, ""
    for _rid, pat, desc in _enabled_patterns(conn_str):
        try:
            if re.search(pat, command):
                return False, desc or pat
        except re.error:
            continue
    return True, ""


def _baseline_check(command: str) -> tuple[bool, str]:
    """仅用内置默认基线匹配（DB 无关，绝不放行已知危险模式）。"""
    for _rid, pat, desc in _compile_default_rules():
        try:
            if re.search(pat, command):
                return False, desc or pat
        except re.error:
            continue
    return True, ""


def check_command_hardened(command: str, conn_str: str | None = None) -> tuple[bool, str]:
    """check_command 的 fail-closed 包装：#1(b) 治本。

    check_command 内部对 DB 故障已回退基线，但调用方过去用 `except: allowed=True` 把【任何异常】
    （import 失败/罕见错误）无条件放行 → rm -rf / 可漏网。这里统一：正常委托 check_command，
    任何异常回退【内置基线】匹配，绝不无条件放行已知危险命令。P0-4/本地执行也复用此入口。
    """
    if not command:
        return True, ""
    try:
        return check_command(command, conn_str)
    except Exception:  # noqa: BLE001
        return _baseline_check(command)


def set_rule_enabled(rule_id: int, enabled: bool, conn_str: str | None = None) -> None:
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE command_blacklist SET enabled = %s WHERE id = %s", (enabled, rule_id))
        conn.commit()
    invalidate_cache()


def add_rule(pattern: str, description: str, conn_str: str | None = None) -> int:
    re.compile(pattern)  # 校验正则，无效则抛
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO command_blacklist (pattern, description, enabled, builtin) "
                "VALUES (%s, %s, true, false) RETURNING id",
                (pattern, description),
            )
            rid = cur.fetchone()[0]
        conn.commit()
    invalidate_cache()
    return rid


def delete_rule(rule_id: int, conn_str: str | None = None) -> bool:
    """删规则。内置规则不可删（只能 disable），返回 False。"""
    with _pooled_conn(conn_str) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT builtin FROM command_blacklist WHERE id = %s", (rule_id,))
            row = cur.fetchone()
            if not row:
                return False
            if row[0]:  # builtin
                return False
            cur.execute("DELETE FROM command_blacklist WHERE id = %s", (rule_id,))
        conn.commit()
    invalidate_cache()
    return True
