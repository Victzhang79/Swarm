#!/usr/bin/env python3
"""brain/l3_gitlab.py 真实单测。

纯函数（gitlab_configured / l3_push_enabled / URL 构造 / 项目路径编码）用 env 驱动；
trigger_and_poll_pipeline 与 create_merge_request 用 mock httpx.Client 验证
触发→轮询→终态判定逻辑，不打真实网络。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain import l3_gitlab

_GITLAB_ENV = [
    "SWARM_GITLAB_URL",
    "SWARM_GITLAB_TOKEN",
    "SWARM_GITLAB_PROJECT_ID",
    "SWARM_GITLAB_TRIGGER_TOKEN",
    "SWARM_GITLAB_REF",
    "SWARM_GITLAB_PUSH_ENABLED",
]


def _set_gitlab(monkeypatch, **kw):
    for k in _GITLAB_ENV:
        monkeypatch.delenv(k, raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


# ── 纯函数 ───────────────────────────────────────


def test_gitlab_configured_true(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    assert l3_gitlab.gitlab_configured() is True
    print("  ✅ gitlab_configured 三项齐全=True")


def test_gitlab_configured_missing(monkeypatch):
    _set_gitlab(monkeypatch, SWARM_GITLAB_URL="https://gitlab.example.com")
    assert l3_gitlab.gitlab_configured() is False
    print("  ✅ gitlab_configured 缺项=False")


def test_l3_push_enabled(monkeypatch):
    _set_gitlab(monkeypatch, SWARM_GITLAB_PUSH_ENABLED="true")
    assert l3_gitlab.l3_push_enabled() is True
    _set_gitlab(monkeypatch, SWARM_GITLAB_PUSH_ENABLED="false")
    assert l3_gitlab.l3_push_enabled() is False
    _set_gitlab(monkeypatch)  # 未设
    assert l3_gitlab.l3_push_enabled() is False
    print("  ✅ l3_push_enabled 开关解析")


def test_project_path_encoded(monkeypatch):
    _set_gitlab(monkeypatch, SWARM_GITLAB_PROJECT_ID="group/sub/proj")
    # 命名空间路径需 URL 编码（/ → %2F）
    assert l3_gitlab._project_path_encoded() == "group%2Fsub%2Fproj"
    print("  ✅ _project_path_encoded 编码命名空间路径")


def test_git_push_remote_url(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="secret",
        SWARM_GITLAB_PROJECT_ID="group/proj",
    )
    url = l3_gitlab._git_push_remote_url()
    assert url == "https://oauth2:secret@gitlab.example.com/group/proj.git"
    print("  ✅ _git_push_remote_url 构造带 token 的 push URL")


def test_git_push_remote_url_unconfigured(monkeypatch):
    _set_gitlab(monkeypatch, SWARM_GITLAB_URL="https://gitlab.example.com")
    assert l3_gitlab._git_push_remote_url() is None
    print("  ✅ _git_push_remote_url 未配置返回 None")


# ── trigger_and_poll_pipeline（mock httpx）──────


def _mock_client_ctx(post_json, get_jsons):
    """构造一个上下文管理器形态的 mock httpx.Client。

    post 返回 post_json；get 依次返回 get_jsons 中的状态。
    """
    client = MagicMock()

    post_resp = MagicMock()
    post_resp.json.return_value = post_json
    post_resp.raise_for_status.return_value = None
    client.post.return_value = post_resp

    get_resps = []
    for body in get_jsons:
        r = MagicMock()
        r.json.return_value = body
        r.raise_for_status.return_value = None
        get_resps.append(r)
    client.get.side_effect = get_resps

    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    return ctx, client


def test_pipeline_success(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    ctx, client = _mock_client_ctx(
        post_json={"id": 1001},
        get_jsons=[{"status": "running"}, {"status": "success"}],
    )
    with patch.object(l3_gitlab.httpx, "Client", return_value=ctx), \
            patch.object(l3_gitlab.time, "sleep", return_value=None):
        ok, msg = l3_gitlab.trigger_and_poll_pipeline(task_id="t1", timeout_sec=60)
    assert ok is True
    assert "1001" in msg and "success" in msg
    print("  ✅ trigger_and_poll_pipeline 轮询到 success")


def test_pipeline_failed(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    ctx, client = _mock_client_ctx(
        post_json={"id": 2002},
        get_jsons=[{"status": "failed", "web_url": "https://gitlab.example.com/p/-/pipelines/2002"}],
    )
    with patch.object(l3_gitlab.httpx, "Client", return_value=ctx), \
            patch.object(l3_gitlab.time, "sleep", return_value=None):
        ok, msg = l3_gitlab.trigger_and_poll_pipeline(task_id="t2", timeout_sec=60)
    assert ok is False
    assert "failed" in msg and "2002" in msg
    print("  ✅ trigger_and_poll_pipeline 终态 failed")


def test_pipeline_no_id(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    ctx, client = _mock_client_ctx(post_json={}, get_jsons=[])
    with patch.object(l3_gitlab.httpx, "Client", return_value=ctx):
        ok, msg = l3_gitlab.trigger_and_poll_pipeline(task_id="t3", timeout_sec=60)
    assert ok is False
    assert "pipeline id" in msg
    print("  ✅ trigger_and_poll_pipeline 无 pipeline id 报错")


def test_pipeline_uses_trigger_token_endpoint(monkeypatch):
    """配置了 trigger token 时走 /trigger/pipeline 端点。"""
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
        SWARM_GITLAB_TRIGGER_TOKEN="trig",
    )
    ctx, client = _mock_client_ctx(
        post_json={"id": 3003},
        get_jsons=[{"status": "success"}],
    )
    with patch.object(l3_gitlab.httpx, "Client", return_value=ctx), \
            patch.object(l3_gitlab.time, "sleep", return_value=None):
        ok, _ = l3_gitlab.trigger_and_poll_pipeline(task_id="t4", timeout_sec=60)
    assert ok is True
    # 验证用了 trigger 端点
    post_url = client.post.call_args[0][0]
    assert post_url.endswith("/trigger/pipeline")
    print("  ✅ trigger_and_poll_pipeline trigger_token 走 /trigger/pipeline")


# ── create_merge_request ─────────────────────────


def test_create_mr_not_configured(monkeypatch):
    _set_gitlab(monkeypatch)  # 全清
    url, err = l3_gitlab.create_merge_request(
        title="t", description="d", source_branch="swarm/x"
    )
    assert url == ""
    assert "not configured" in err
    print("  ✅ create_merge_request 未配置直接返回")


def test_create_mr_success(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"web_url": "https://gitlab.example.com/p/-/merge_requests/7"}
    resp.raise_for_status.return_value = None
    client = MagicMock()
    client.post.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    with patch.object(l3_gitlab.httpx, "Client", return_value=ctx):
        url, err = l3_gitlab.create_merge_request(
            title="t", description="d", source_branch="swarm/x", task_id="task-1"
        )
    assert err == ""
    assert url.endswith("/merge_requests/7")
    print("  ✅ create_merge_request 成功返回 web_url")


def test_create_mr_conflict(monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    resp = MagicMock()
    resp.status_code = 409
    client = MagicMock()
    client.post.return_value = resp
    ctx = MagicMock()
    ctx.__enter__.return_value = client
    ctx.__exit__.return_value = False
    with patch.object(l3_gitlab.httpx, "Client", return_value=ctx):
        url, err = l3_gitlab.create_merge_request(
            title="t", description="d", source_branch="swarm/x"
        )
    assert url == ""
    assert "already exists" in err
    print("  ✅ create_merge_request 409 已存在")


# ── push_merged_diff_branch 边界 ─────────────────


def test_push_empty_diff():
    branch, err = l3_gitlab.push_merged_diff_branch("/tmp", "", "task-1")
    assert branch is None
    assert "empty" in err
    print("  ✅ push_merged_diff_branch 空 diff 直接拒绝")


def test_push_not_git_repo(tmp_path, monkeypatch):
    _set_gitlab(
        monkeypatch,
        SWARM_GITLAB_URL="https://gitlab.example.com",
        SWARM_GITLAB_TOKEN="tok",
        SWARM_GITLAB_PROJECT_ID="42",
    )
    branch, err = l3_gitlab.push_merged_diff_branch(
        str(tmp_path), "--- a/x\n+++ b/x\n", "task-1"
    )
    assert branch is None
    assert "not a git repository" in err
    print("  ✅ push_merged_diff_branch 非 git 仓库报错")


if __name__ == "__main__":
    import inspect

    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                continue  # 需 fixture 的跳过
            fn()
    print("\nl3_gitlab 单测通过。")
