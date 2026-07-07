"""L3 — GitLab CI/CD pipeline 触发与状态回流（Phase 5 P0）。"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import quote

import httpx

from swarm.project.diff_apply import diff_paths_escape_root

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
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_path,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _resolve_l3_base(
    project_path: str, base_ref: str, base_commit: str | None
) -> tuple[str | None, str]:
    """解析 L3 apply 的基线 commit（round29 口径：优先与 merged_diff 生成同源的钉扎 base）。

    顺序：钉扎 base_commit → origin/<base_ref> → <base_ref>。返回 (sha, 使用的引用名)。
    """
    candidates = ([base_commit] if base_commit else []) + [f"origin/{base_ref}", base_ref]
    for cand in candidates:
        rp = _run_git(project_path, ["rev-parse", "--verify", f"{cand}^{{commit}}"], timeout=30)
        sha = (rp.stdout or "").strip()
        if rp.returncode == 0 and sha:
            return sha, cand
    return None, ""


def push_merged_diff_branch(
    project_path: str,
    merged_diff: str,
    task_id: str,
    *,
    base_ref: str | None = None,
    base_commit: str | None = None,
) -> tuple[str | None, str]:
    """把 merged_diff 应用到【纯净 base 树】烤成提交并 push 到 GitLab——完全不碰工作树。

    D34 治本（round29 merge_engine._apply_check_against_base 同口径）：旧实现在真实工作树
    `checkout -B` 后 apply——pull-back 已把 merged_diff 要"新建"的文件材化进工作树（untracked，
    checkout 不清）→ create 补丁撞 "already exists" apply 必败，而 verify_l3 旧逻辑随后回退
    默认 ref 跑 pipeline 假绿。现改为 git 底层管道：`read-tree <base>` 进【临时 index】→
    `git apply --cached --ignore-whitespace`（对 base 树校验+应用，与 diff 生成基线同源）→
    `write-tree`/`commit-tree` → push 该提交。工作树/真 index/当前分支零改动，untracked
    脏文件与"补丁是否合法"彻底解耦。base_commit＝任务钉扎基线（BrainState.base_commit），
    优先于 origin/<base_ref>，保证应用基线与 merged_diff 生成基线同源。

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

    # fail-closed 越界预检：不再经 apply_git_diff（其内建边界检查随之失效），此处补回对称防线。
    _escaped = diff_paths_escape_root(project_path, merged_diff)
    if _escaped:
        return None, f"merged_diff 含越界路径(逃出工作区)，fail-closed 拒绝: {_escaped[:5]}"

    base_ref = base_ref or os.environ.get("SWARM_GITLAB_REF", "main")
    safe_id = "".join(c if c.isalnum() or c in "-_" else "-" for c in task_id)[:24]
    branch = f"swarm/l3-{safe_id or 'task'}"

    try:
        fetch = _run_git(project_path, ["fetch", "origin", base_ref], timeout=180)
        if fetch.returncode != 0:
            logger.debug("[L3 push] fetch origin/%s: %s", base_ref, fetch.stderr.strip())

        base_sha, base_used = _resolve_l3_base(project_path, base_ref, base_commit)
        if not base_sha:
            return None, (
                f"cannot resolve L3 base (tried base_commit={base_commit or '-'}, "
                f"origin/{base_ref}, {base_ref})"
            )

        with tempfile.TemporaryDirectory(prefix="l3idx_") as td:
            idx = os.path.join(td, "index")
            env = {
                **os.environ,
                "GIT_INDEX_FILE": idx,
                # commit-tree 需要身份；L3 验证分支是 swarm 自有 scratch 产物，固定身份即可。
                "GIT_AUTHOR_NAME": "swarm", "GIT_AUTHOR_EMAIL": "swarm@localhost",
                "GIT_COMMITTER_NAME": "swarm", "GIT_COMMITTER_EMAIL": "swarm@localhost",
            }
            rt = _run_git(project_path, ["read-tree", base_sha], timeout=60, env=env)
            if rt.returncode != 0:
                return None, f"git read-tree {base_sha[:12]} failed: {rt.stderr.strip()}"

            # git 要求补丁以换行结尾（否则末行判 corrupt patch）；bytes 写避免改写 CRLF。
            patch_path = os.path.join(td, "l3.patch")
            patch_bytes = merged_diff.encode("utf-8")
            if not patch_bytes.endswith(b"\n"):
                patch_bytes += b"\n"
            with open(patch_path, "wb") as pf:
                pf.write(patch_bytes)

            # --ignore-whitespace 与真实交付 apply(project/diff_apply)同旗标（CRLF 项目↔LF diff）。
            ap = _run_git(
                project_path,
                ["apply", "--cached", "--ignore-whitespace", patch_path],
                timeout=120, env=env,
            )
            if ap.returncode != 0:
                return None, f"git apply (cached, base={base_used}) failed: {ap.stderr.strip()}"

            wt = _run_git(project_path, ["write-tree"], timeout=60, env=env)
            if wt.returncode != 0:
                return None, f"git write-tree failed: {wt.stderr.strip()}"
            tree = (wt.stdout or "").strip()

            ct = _run_git(
                project_path,
                ["commit-tree", tree, "-p", base_sha, "-m", f"swarm: L3 verify {task_id}"],
                timeout=60, env=env,
            )
            if ct.returncode != 0:
                return None, f"git commit-tree failed: {ct.stderr.strip()}"
            commit_sha = (ct.stdout or "").strip()

        # --force：swarm/l3-* 是任务专属 scratch 验证分支；重试/replan 产生的新提交不与上次
        # 同链（都直接基于 base），非 FF 拒绝会让重跑 L3 永久失败——覆盖推送是此分支的语义。
        push = _run_git(
            project_path,
            [*_gitlab_auth_header_args(), "push", "--force", remote_url,
             f"{commit_sha}:refs/heads/{branch}"],
            timeout=300,
        )
        if push.returncode != 0:
            return None, f"git push failed: {_redact_secrets(push.stderr.strip())}"

        logger.info(
            "[L3 push] 分支 %s 已推送 (base=%s@%s, 临时 index base 树口径，工作树零改动)",
            branch, base_used, base_sha[:12],
        )
        return branch, ""
    except subprocess.TimeoutExpired:
        return None, "git operation timeout"
    except Exception as exc:
        return None, _redact_secrets(str(exc))


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
