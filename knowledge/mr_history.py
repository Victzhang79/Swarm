"""Layer D 扩展 — GitLab MR 历史索引。"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

MR_HISTORY_DDL = """
CREATE TABLE IF NOT EXISTS kb_mr_history (
    id              BIGSERIAL PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    mr_iid          INT         NOT NULL,
    title           TEXT,
    description     TEXT,
    author          TEXT,
    state           TEXT,
    web_url         TEXT,
    changed_files   JSONB       DEFAULT '[]',
    merged_at       TIMESTAMPTZ,
    metadata_json   JSONB       DEFAULT '{}',
    UNIQUE(project_id, mr_iid)
);

CREATE INDEX IF NOT EXISTS idx_mr_project ON kb_mr_history(project_id, merged_at DESC);
"""


async def sync_mr_history_from_gitlab(
    store_conn_factory,
    project_id: str,
    *,
    limit: int = 100,
) -> int:
    """从 GitLab 拉取最近 MR 写入 kb_mr_history。"""
    base = os.environ.get("SWARM_GITLAB_URL", "").rstrip("/")
    token = os.environ.get("SWARM_GITLAB_TOKEN", "")
    gitlab_project = os.environ.get("SWARM_GITLAB_PROJECT_ID", "").strip()
    if not base or not token or not gitlab_project:
        return 0

    encoded = quote(gitlab_project, safe="")
    url = f"{base}/api/v4/projects/{encoded}/merge_requests"
    headers = {"PRIVATE-TOKEN": token}
    count = 0

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                url,
                headers=headers,
                params={"state": "merged", "order_by": "updated_at", "sort": "desc", "per_page": limit},
            )
            resp.raise_for_status()
            mrs = resp.json()
    except Exception as exc:
        logger.warning("[MR history] fetch failed: %s", exc)
        return 0

    conn = store_conn_factory()
    if hasattr(conn, "__await__"):
        conn = await conn
    try:
        async with conn.cursor() as cur:
            for mr in mrs:
                iid = mr.get("iid")
                if not iid:
                    continue
                changed: list[str] = []
                try:
                    ch_url = f"{base}/api/v4/projects/{encoded}/merge_requests/{iid}/changes"
                    with httpx.Client(timeout=20.0) as client:
                        cr = client.get(ch_url, headers=headers)
                        if cr.status_code == 200:
                            for ch in cr.json().get("changes") or []:
                                if ch.get("new_path"):
                                    changed.append(ch["new_path"])
                                elif ch.get("old_path"):
                                    changed.append(ch["old_path"])
                except Exception as exc:
                    logger.debug("解析 MR changed_files 失败: %s", exc)

                await cur.execute(
                    """
                    INSERT INTO kb_mr_history
                        (project_id, mr_iid, title, description, author, state, web_url, changed_files, merged_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, mr_iid) DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        changed_files = EXCLUDED.changed_files,
                        merged_at = EXCLUDED.merged_at
                    """,
                    (
                        project_id,
                        iid,
                        mr.get("title"),
                        (mr.get("description") or "")[:4000],
                        (mr.get("author") or {}).get("username"),
                        mr.get("state"),
                        mr.get("web_url"),
                        changed,
                        mr.get("merged_at"),
                    ),
                )
                count += 1
    finally:
        await conn.close()
    return count


async def query_mr_history_for_files(
    cur,
    project_id: str,
    files: list[str],
    *,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """查询与给定文件相关的 MR 历史。"""
    if not files:
        return []
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    for fp in files[:5]:
        await cur.execute(
            """
            SELECT mr_iid, title, author, web_url, changed_files, merged_at
            FROM kb_mr_history
            WHERE project_id = %s AND changed_files::text ILIKE %s
            ORDER BY merged_at DESC NULLS LAST
            LIMIT %s
            """,
            (project_id, f"%{fp}%", top_k),
        )
        for row in await cur.fetchall():
            iid = row[0]
            if iid in seen:
                continue
            seen.add(iid)
            results.append({
                "mr_iid": iid,
                "title": row[1],
                "author": row[2],
                "web_url": row[3],
                "changed_files": row[4],
                "merged_at": str(row[5]) if row[5] else None,
                "trigger_file": fp,
                "type": "mr_history",
            })
    return results[:top_k]
