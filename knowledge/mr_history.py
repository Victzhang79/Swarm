"""Layer D 扩展 — GitLab MR 历史索引。"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from swarm.knowledge.project_fence import (
    ProjectKnowledgeUpdateRejected,
    ProjectKnowledgeWriterBusy,
    project_knowledge_write_fence,
)

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


def project_matches_gitlab_mapping(
    project: dict[str, Any],
    gitlab_url: str,
    gitlab_project: str,
    resolved_project_path: str | None = None,
) -> bool:
    """确定本地项目是否对应当前 GitLab project。

    显式 `config.gitlab_project_id` 优先；存量/新项目没有该字段时，从本地
    origin 的 host + group/repo 确定性对账，避免把全局 MR 广播给无关项目。
    """
    config = project.get("config") if isinstance(project.get("config"), dict) else {}
    explicit = str(config.get("gitlab_project_id") or "").strip().strip("/")
    project_identifier = gitlab_project.strip().strip("/").removesuffix(".git")
    target = (resolved_project_path or project_identifier).strip().strip("/").removesuffix(".git")
    if explicit:
        return explicit.removesuffix(".git") in {project_identifier, target}
    project_path = str(project.get("path") or "").strip()
    if not project_path:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    remote = (proc.stdout or "").strip()
    if "://" in remote:
        parsed = urlparse(remote)
        remote_host = (parsed.hostname or "").lower()
        remote_path = parsed.path.strip("/")
    elif "@" in remote and ":" in remote:
        host_part, remote_path = remote.rsplit(":", 1)
        remote_host = host_part.rsplit("@", 1)[-1].lower()
        remote_path = remote_path.strip("/")
    else:
        return False
    configured_host = (urlparse(gitlab_url).hostname or "").lower()
    return bool(
        configured_host
        and remote_host == configured_host
        and remote_path.removesuffix(".git") == target
    )


async def resolve_gitlab_project_path(
    gitlab_url: str,
    token: str,
    gitlab_project: str,
) -> str | None:
    """把 GitLab 数字 project ID 解析为 path_with_namespace，非数字路径原样返回。"""
    identifier = gitlab_project.strip().strip("/")
    if not identifier:
        return None
    if not identifier.isdigit():
        return identifier.removesuffix(".git")
    if not gitlab_url or not token:
        return None
    url = f"{gitlab_url.rstrip('/')}/api/v4/projects/{quote(identifier, safe='')}"
    try:
        response = await asyncio.to_thread(
            httpx.get,
            url,
            headers={"PRIVATE-TOKEN": token},
            timeout=20.0,
        )
        response.raise_for_status()
        resolved = str(response.json().get("path_with_namespace") or "").strip()
        return resolved or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MR history] 解析 GitLab 数字 project ID %s 失败: %s", identifier, exc)
        return None


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

    # A-P1-24：单个 httpx.Client 复用到整个循环（含每 MR 的 /changes），避免每 MR 新建
    # 连接；同时 /changes 显式处理非 200（429/401）—— 原先 status_code != 200 静默落库
    # 空 changed_files，会把"被限流/鉴权失败"伪装成"该 MR 没改文件"，污染共现检索。
    try:
        client = httpx.Client(timeout=30.0)
    except Exception as exc:
        logger.warning("[MR history] client init failed: %s", exc)
        return 0

    prepared: list[tuple[Any, ...]] = []
    try:
        try:
            resp = await asyncio.to_thread(
                client.get,
                url,
                headers=headers,
                params={"state": "merged", "order_by": "updated_at", "sort": "desc", "per_page": limit},
            )
            resp.raise_for_status()
            mrs = resp.json()
        except Exception as exc:
            logger.warning("[MR history] fetch failed: %s", exc)
            return 0

        # 所有外部 HTTP 都在项目写围栏之外完成。单个 /changes 失败仍按既有语义
        # 跳过该 MR；成功结果规范化为纯参数快照，围栏内不再有远端等待。
        for mr in mrs:
            iid = mr.get("iid")
            if not iid:
                continue
            changed: list[str] = []
            try:
                ch_url = f"{base}/api/v4/projects/{encoded}/merge_requests/{iid}/changes"
                cr = await asyncio.to_thread(
                    client.get, ch_url, headers=headers, timeout=20.0
                )
                cr.raise_for_status()
                for ch in cr.json().get("changes") or []:
                    if ch.get("new_path"):
                        changed.append(ch["new_path"])
                    elif ch.get("old_path"):
                        changed.append(ch["old_path"])
            except Exception as exc:
                logger.warning(
                    "[MR history] MR %s /changes 拉取失败(%s)，跳过 changed_files 更新",
                    iid, exc,
                )
                continue
            prepared.append((
                project_id,
                iid,
                mr.get("title"),
                (mr.get("description") or "")[:4000],
                (mr.get("author") or {}).get("username"),
                mr.get("state"),
                mr.get("web_url"),
                changed,
                mr.get("merged_at"),
            ))
    finally:
        client.close()

    if not prepared:
        return 0
    try:
        async with project_knowledge_write_fence(
            project_id,
            operation="同步 MR 历史",
        ):
            conn = store_conn_factory()
            if hasattr(conn, "__await__"):
                conn = await conn
            try:
                async with conn.cursor() as cur:
                    for params in prepared:
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
                            params,
                        )
                        count += 1
            finally:
                await conn.close()
    except (ProjectKnowledgeUpdateRejected, ProjectKnowledgeWriterBusy) as exc:
        logger.info("[MR history] project=%s 写入让位: %s", project_id, exc)
        return 0
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
