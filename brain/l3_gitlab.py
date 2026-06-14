"""L3 — GitLab CI/CD pipeline 触发与状态回流（Phase 5 P0）。"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from typing import Any
from urllib.parse import quote

import httpx

from swarm.project.diff_apply import apply_git_diff

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 5
DEFAULT_TIMEOUT_SEC = 600


def gitlab_configured() -> bool:
    return bool(
        os.environ.get("SWARM_GITLAB_URL", "").strip()
        and os.environ.get("SWARM_GITLAB_TOKEN", "").strip()
        and os.environ.get("SWARM_GITLAB_PROJECT_ID", "").strip()
    )


def l3_push_enabled() -> bool:
    return os.environ.get("SWARM_GITLAB_PUSH_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
    )


def _project_path_encoded() -> str:
    raw = os.environ.get("SWARM_GITLAB_PROJECT_ID", "").strip()
    return quote(raw, safe="")


def _git_push_remote_url() -> str | None:
    """构造 GitLab HTTPS push URL（不含凭证）。

    安全（audit #40）：token 不再拼进 URL（避免进程参数/stderr/日志泄漏），
    改由 push 时经 `git -c http.extraHeader` 注入 Authorization。
    """
    base = os.environ.get("SWARM_GITLAB_URL", "").rstrip("/")
    token = os.environ.get("SWARM_GITLAB_TOKEN", "").strip()
    project = os.environ.get("SWARM_GITLAB_PROJECT_ID", "").strip()
    if not base or not token or not project:
        return None
    host = base.replace("https://", "").replace("http://", "")
    return f"https://{host}/{project}.git"


def _redact_secrets(text: str) -> str:
    """从 git 输出/错误信息中抹除 token，避免泄漏进日志/返回值（audit #40）。"""
    if not text:
        return text
    token = os.environ.get("SWARM_GITLAB_TOKEN", "").strip()
    if token and token in text:
        text = text.replace(token, "***")
    # 兜底：oauth2:xxx@ 形式（兼容历史 URL 残留）
    import re
    text = re.sub(r"(oauth2:)[^@]+(@)", r"\1***\2", text)
    return text


def _gitlab_auth_header_args() -> list[str]:
    """返回 push 用的 `-c http.extraHeader=...` 参数（token 不进 URL/不进命令位置参数前缀）。"""
    token = os.environ.get("SWARM_GITLAB_TOKEN", "").strip()
    if not token:
        return []
    return ["-c", f"http.extraHeader=Authorization: Bearer {token}"]


def _run_git(
    project_path: str,
    args: list[str],
    *,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def push_merged_diff_branch(
    project_path: str,
    merged_diff: str,
    task_id: str,
    *,
    base_ref: str | None = None,
) -> tuple[str | None, str]:
    """在临时分支应用 merged_diff 并 push 到 GitLab。

    Returns:
        (branch_name, error_message) — 成功时 error 为空字符串。
    """
    if not merged_diff.strip():
        return None, "empty merged_diff"

    git_dir = os.path.join(project_path, ".git")
    if not os.path.isdir(git_dir):
        return None, f"not a git repository: {project_path}"

    remote_url = _git_push_remote_url()
    if not remote_url:
        return None, "GitLab push URL 未配置（需 SWARM_GITLAB_URL/TOKEN/PROJECT_ID）"

    base_ref = base_ref or os.environ.get("SWARM_GITLAB_REF", "main")
    safe_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in task_id)[:24]
    branch = f"swarm/l3-{safe_id or 'task'}"

    try:
        fetch = _run_git(project_path, ["fetch", "origin", base_ref], timeout=180)
        if fetch.returncode != 0:
            logger.debug("[L3 push] fetch origin/%s: %s", base_ref, fetch.stderr.strip())

        checkout = _run_git(
            project_path,
            ["checkout", "-B", branch, f"origin/{base_ref}"],
            timeout=60,
        )
        if checkout.returncode != 0:
            checkout = _run_git(project_path, ["checkout", "-B", branch, base_ref], timeout=60)
        if checkout.returncode != 0:
            return None, f"git checkout failed: {checkout.stderr.strip()}"

        apply_result = apply_git_diff(project_path, merged_diff)
        if not apply_result.get("ok"):
            _run_git(project_path, ["checkout", "-"], timeout=30)
            return None, f"git apply failed: {apply_result.get('stderr', apply_result)}"

        status = _run_git(project_path, ["status", "--porcelain"], timeout=30)
        if status.returncode != 0:
            return None, f"git status failed: {status.stderr.strip()}"

        if not (status.stdout or "").strip():
            logger.info("[L3 push] diff 应用后无变更，仍推送分支 %s", branch)
        else:
            _run_git(project_path, ["add", "-A"], timeout=60)
            commit = _run_git(
                project_path,
                ["commit", "-m", f"swarm: L3 verify {task_id}"],
                timeout=60,
            )
            if commit.returncode != 0:
                return None, f"git commit failed: {commit.stderr.strip()}"

        push = _run_git(
            project_path,
            [*_gitlab_auth_header_args(), "push", remote_url, f"HEAD:{branch}"],
            timeout=300,
        )
        if push.returncode != 0:
            return None, f"git push failed: {_redact_secrets(push.stderr.strip())}"

        logger.info("[L3 push] 分支 %s 已推送 (base=%s)", branch, base_ref)
        return branch, ""
    except subprocess.TimeoutExpired:
        return None, "git operation timeout"
    except Exception as exc:
        return None, _redact_secrets(str(exc))
    finally:
        try:
            _run_git(project_path, ["checkout", "-"], timeout=30)
        except Exception:
            pass


def trigger_and_poll_pipeline(
    *,
    task_id: str,
    ref: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """触发 GitLab pipeline 并轮询至 success/failed/canceled/timeout。"""
    base = os.environ.get("SWARM_GITLAB_URL", "").rstrip("/")
    token = os.environ.get("SWARM_GITLAB_TOKEN", "")
    trigger_token = os.environ.get("SWARM_GITLAB_TRIGGER_TOKEN", "").strip()
    project = _project_path_encoded()
    ref = ref or os.environ.get("SWARM_GITLAB_REF", "main")

    headers = {"PRIVATE-TOKEN": token}
    variables = {"SWARM_TASK_ID": task_id}

    with httpx.Client(timeout=30.0, verify=True) as client:
        if trigger_token:
            url = f"{base}/api/v4/projects/{project}/trigger/pipeline"
            resp = client.post(
                url,
                data={"token": trigger_token, "ref": ref, **{f"variables[{k}]": v for k, v in variables.items()}},
            )
        else:
            url = f"{base}/api/v4/projects/{project}/pipeline"
            resp = client.post(
                url,
                headers=headers,
                json={"ref": ref, "variables": [{"key": k, "value": v} for k, v in variables.items()]},
            )
        resp.raise_for_status()
        pipeline: dict[str, Any] = resp.json()
        pipeline_id = pipeline.get("id")
        if not pipeline_id:
            return False, "GitLab 未返回 pipeline id"

        logger.info("[L3 GitLab] pipeline #%s triggered ref=%s task=%s", pipeline_id, ref, task_id)
        deadline = time.monotonic() + timeout_sec
        status_url = f"{base}/api/v4/projects/{project}/pipelines/{pipeline_id}"

        while time.monotonic() < deadline:
            pr = client.get(status_url, headers=headers)
            pr.raise_for_status()
            data = pr.json()
            status = (data.get("status") or "").lower()
            if status == "success":
                return True, f"GitLab pipeline #{pipeline_id} success"
            if status in ("failed", "canceled", "skipped"):
                web_url = data.get("web_url", "")
                return False, f"GitLab pipeline #{pipeline_id} {status}" + (f" ({web_url})" if web_url else "")
            time.sleep(POLL_INTERVAL_SEC)

        return False, f"GitLab pipeline #{pipeline_id} timeout after {timeout_sec}s"


def create_merge_request(
    *,
    title: str,
    description: str,
    source_branch: str,
    task_id: str = "",
    target_branch: str | None = None,
) -> tuple[str, str]:
    """创建 GitLab MR（草稿）。Returns (web_url, error_message)。"""
    if not gitlab_configured():
        return "", "GitLab not configured"

    base = os.environ.get("SWARM_GITLAB_URL", "").rstrip("/")
    token = os.environ.get("SWARM_GITLAB_TOKEN", "")
    project = _project_path_encoded()
    target_branch = target_branch or os.environ.get("SWARM_GITLAB_REF", "main")
    headers = {"PRIVATE-TOKEN": token}

    try:
        with httpx.Client(timeout=30.0, verify=True) as client:
            url = f"{base}/api/v4/projects/{project}/merge_requests"
            resp = client.post(
                url,
                headers=headers,
                json={
                    "title": title,
                    "description": description,
                    "source_branch": source_branch,
                    "target_branch": target_branch,
                    "remove_source_branch": True,
                    "draft": True,
                },
            )
            if resp.status_code == 409:
                return "", f"MR already exists for branch {source_branch}"
            resp.raise_for_status()
            data = resp.json()
            web_url = data.get("web_url", "")
            logger.info("[L3 GitLab] MR created task=%s url=%s", task_id, web_url)
            return web_url, ""
    except Exception as exc:
        return "", str(exc)
