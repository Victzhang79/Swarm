"""配置变更审计（C1-C）——who / when / key / old→new / 来源端点。

病灶（26 号文调查）：改配置**零审计**。`api/routers/config.py` 通篇无 audit 写入，
唯一痕迹是一行 `logger.info("Updated .env + os.environ with keys: [...]")`——**不记 who、
不记 old→new**（只有非 admin 被拒时才记 who）。于是"谁在什么时候把这个闸关了"无法回答，
改坏了也没有回滚点。`task_audit_log` 是任务维度的，管不到配置面。

设计取舍：
  - **值一律脱敏后入库**（只留前 4 字符 + 长度）。这张表本身绝不能成为新的泄露面——
    26 号文 S-3 的教训是"以为安全的地方存了明文凭据"，审计表比 .env 更容易被导出。
  - **append-only、写失败不阻断配置变更**：审计是旁路观测，不能因为它挂了就改不了配置
    （fail-open 方向在此是正确的——对照：入库准入闸是 fail-closed，因为那是安全闸）。
    但失败必须 WARNING 留痕。
  - 不做保留期清理逻辑：随 task_audit_log 同族由 api/app.py 的每日保留策略统一处理。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# ★脱敏谓词与 api/_shared.py:_mask_config_dict 同源（复核 BLOCKER-4：单一事实源）★
# 初版用 `KEY/TOKEN/SECRET/...` 裸子串匹配键名，实测把 **9 个非凭据键**也抹掉了，
# 其中三个是安全开关：`SWARM_REQUIRE_SECRET_KEY`、`SWARM_SSH_STRICT_HOST_KEY`、
# `SWARM_ALLOW_LEGACY_API_KEY`——它们的值就是 0/1，抹成 `***(len=1)` 后
# "谁把 REQUIRE_SECRET_KEY 从 1 关成 0"这条记录**与没记完全等价**，
# 而"记录安全开关被谁改了"正是这张表的首要立项理由。
_SECRETY = ("api_key", "apikey", "secret", "password", "passwd", "credential", "token")

# 布尔/数值型值没有秘密可泄，且正是最需要看清 old→new 的那类（开关、阈值、超时）
_NON_SECRET_VALUE_RE = re.compile(r"^(?:\d+(?:\.\d+)?|true|false|yes|no|on|off)$", re.I)


def mask_value(key: str, value: str | None) -> str | None:
    """脱敏：凭据类只留 `***(len=N)`；其余留前 4 字符 + 长度。None 原样（表示"此前无此键"）。

    例外：值是纯布尔/数值时**留原值**——`SWARM_REQUIRE_SECRET_KEY=0` 这种记录必须能看出
    改成了什么，否则审计对开关类变更失效（复核 BLOCKER-4）。
    """
    if value is None:
        return None
    v = str(value)
    if not v:
        return ""
    if _NON_SECRET_VALUE_RE.match(v.strip()):
        return v
    if any(t in key.lower() for t in _SECRETY):
        return f"***(len={len(v)})"
    return (v[:4] + f"…(len={len(v)})") if len(v) > 4 else v


def record_config_changes(
    who: str, source: str, changes: dict[str, tuple[str | None, str]],
    conn_str: str | None = None,
) -> int:
    """记录一批配置变更；返回成功写入的条数。

    changes: {key: (old_or_None, new)}。**只记真正发生变化的键**——调用方应先做差集，
    否则每次保存都会灌进一堆 old==new 的噪声行，把真正的变更淹掉。
    """
    try:
        # 脱敏也在 try 内：审计的【任何一步】失败都不得阻断配置变更本身。
        # （初版把 rows 计算放在 try 外，测试实证 mask_value 一抛就会传播出去打断变更。）
        rows = [(who, source, k, mask_value(k, old), mask_value(k, new))
                for k, (old, new) in (changes or {}).items() if old != new]
        if not rows:
            return 0

        import psycopg

        from swarm.config.settings import DatabaseConfig
        from swarm.infra.db import pg_connect_timeout_kwargs
        dsn = conn_str or DatabaseConfig().postgres_uri
        # ★必须带 connect_timeout（复核 S2）★：本函数绕过连接池直连，而 D15 的
        # pg_connect_timeout_kwargs 正是为这种直连准备的唯一取值点。不带的话，PG 网络
        # 黑洞（丢包挂起而非 refused）会让 connect 无限挂——本函数跑在 to_thread 里，
        # 线程被永久占用，反复改配置即耗尽 executor，事件循环上所有 to_thread 排队 →
        # 全站冻结。"审计绝不阻断配置变更"在挂起场景下并不成立：变更生效了，但调用方
        # 永远拿不到响应。fail-open 必须是【快速】fail-open。
        with psycopg.connect(dsn, **pg_connect_timeout_kwargs()) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO config_audit_log (who, source, key_name, old_masked, new_masked) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    rows,
                )
        logger.info("[CONFIG-AUDIT] by=%s source=%s 记录 %d 条配置变更: %s",
                    who, source, len(rows), [r[2] for r in rows][:10])
        return len(rows)
    except Exception as exc:  # noqa: BLE001 — 审计是旁路，绝不阻断配置变更本身
        logger.warning("[CONFIG-AUDIT] 写审计失败（配置变更本身已生效，不回滚）by=%s source=%s: %s",
                       who, source, exc)
        return 0
