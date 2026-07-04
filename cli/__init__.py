"""Swarm CLI — 命令行交互入口"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import click
import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

DEFAULT_API_URL = os.environ.get("SWARM_API_URL", "http://127.0.0.1:8420")

# CLI token 缓存路径（swarm login 写入，各命令自动读取）
_TOKEN_CACHE = os.path.expanduser("~/.swarm/cli_token")


def _load_token() -> str:
    """读取 CLI token：环境变量 SWARM_TOKEN 优先，回退 ~/.swarm/cli_token 缓存。"""
    tok = os.environ.get("SWARM_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open(_TOKEN_CACHE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _auth_headers(extra: dict | None = None) -> dict:
    """构造带 Bearer token 的请求头（rbac_enabled=true 时必需）。"""
    headers = dict(extra or {})
    tok = _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


# #6 round22：sync httpx 调用统一包装——始终注入 _auth_headers()，401 时提示 login。
# 过去多数命令裸调 httpx.get/post/... 漏带 token → RBAC 开启时 401（功能坏）。包装幂等：
# 已显式传 headers 也会合并 Authorization，不冲突、不丢自定义头。
def _http(method: str, url: str, **kw):
    kw["headers"] = _auth_headers(kw.get("headers"))
    resp = getattr(httpx, method)(url, **kw)
    if getattr(resp, "status_code", None) == 401:
        click.echo("⚠️  未授权（401）。请先执行 `swarm login` 获取 token。", err=True)
    return resp


def _hget(url: str, **kw):
    return _http("get", url, **kw)


def _hpost(url: str, **kw):
    return _http("post", url, **kw)


def _hput(url: str, **kw):
    return _http("put", url, **kw)


def _hdelete(url: str, **kw):
    return _http("delete", url, **kw)


@click.group()
@click.version_option(version="0.9.9", prog_name="swarm")
def main():
    """🐝 Swarm — 蜂群 AI 编程智能体系统"""
    # 统一日志（CLI 本地执行 worker/check 等命令时也走轮转文件 + task 上下文）
    try:
        from swarm.logging_config import setup_logging

        setup_logging()
    except Exception:
        pass


@main.command()
@click.option("--username", "-u", default="admin", show_default=True, help="用户名")
@click.option("--password", "-P", default=None, help="密码（留空则交互输入）")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def login(username: str, password: str | None, api_url: str):
    """登录并缓存 token 到 ~/.swarm/cli_token（rbac 开启时各命令依赖）"""
    api_url = api_url.rstrip("/")
    if not password:
        password = click.prompt("密码", hide_input=True, default="", show_default=False)
    try:
        resp = _hpost(
            f"{api_url}/api/auth/login",
            json={"username": username, "password": password},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("token", "")
        if not token:
            console.print("[red]登录响应无 token[/]")
            sys.exit(1)
        os.makedirs(os.path.dirname(_TOKEN_CACHE), exist_ok=True)
        with open(_TOKEN_CACHE, "w", encoding="utf-8") as f:
            f.write(token)
        os.chmod(_TOKEN_CACHE, 0o600)
        user = data.get("user", {})
        console.print(
            f"[green]✅ 已登录 {user.get('username', username)} "
            f"({user.get('global_role', '?')}) — token 已缓存[/]"
        )
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]登录失败: {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API ({api_url}): {exc}[/]")
        sys.exit(1)


@main.command()
@click.argument("description")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--watch", "-w", is_flag=True, help="实时跟踪任务执行（SSE）")
@click.option("--auto-accept", is_flag=True, help="自动通过人工审核")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True, help="Swarm API 地址")
def submit(description: str, project: str, watch: bool, auto_accept: bool, api_url: str):
    """提交一个编程任务（经 API 启动 Brain）"""
    console.print(Panel(
        f"[bold blue]🐝 提交任务[/]\n\n项目: {project}\n描述: {description}\nAPI: {api_url}"
    ))
    asyncio.run(_submit_via_api(description, project, watch, auto_accept, api_url.rstrip("/")))


async def _submit_via_api(
    description: str,
    project: str,
    watch: bool,
    auto_accept: bool,
    api_url: str,
) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=_auth_headers()) as client:
        try:
            resp = await client.post(
                f"{api_url}/api/projects/{project}/tasks",
                json={"description": description, "auto_accept": auto_accept},
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            console.print(f"[red]创建任务失败 ({exc.response.status_code}): {detail}[/]")
            sys.exit(1)
        except httpx.RequestError as exc:
            console.print(f"[red]无法连接 API ({api_url}): {exc}[/]")
            sys.exit(1)

        payload = resp.json()
        task = payload.get("task") or payload
        task_id = task.get("id")
        if not task_id:
            console.print("[red]API 未返回 task_id[/]")
            sys.exit(1)

        console.print(f"[green]✅ 任务已创建[/] id={task_id}")
        if not watch:
            console.print("[dim]使用 --watch 跟踪进度，或打开 Web UI 查看[/dim]")
            return

        console.print("[dim]订阅 SSE 进度流…[/dim]")
        stream_url = f"{api_url}/api/tasks/{task_id}/stream"
        try:
            async with httpx.AsyncClient(timeout=None, headers=_auth_headers()) as stream_client:
                async with stream_client.stream("GET", stream_url) as stream:
                    stream.raise_for_status()
                    event_type = "progress"
                    async for raw_line in stream.aiter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if line.startswith("event:"):
                            event_type = line.split(":", 1)[1].strip()
                            continue
                        if not line.startswith("data:"):
                            continue
                        data_str = line.split(":", 1)[1].strip()
                        if not data_str:
                            continue
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            console.print(f"[dim]{data_str}[/]")
                            continue

                        msg = data.get("message") or data.get("msg") or ""
                        step = data.get("step") or event_type
                        if msg:
                            console.print(f"[cyan]{step}[/] {msg}")
                        elif step:
                            console.print(f"[cyan]{step}[/]")

                        if step in ("complete", "awaiting_review") or event_type == "result":
                            if data.get("result"):
                                result = data["result"]
                                diff = result.get("merged_diff") or ""
                                console.print(Panel(
                                    f"状态: {result.get('status', 'N/A')}\n"
                                    f"Diff 行数: {len(diff.splitlines()) if diff else 0}",
                                    title="📊 执行结果",
                                ))
                            if step == "complete":
                                break
                        if step == "error" or event_type == "error":
                            console.print(f"[red]❌ {msg or '任务失败'}[/]")
                            sys.exit(1)
        except httpx.HTTPError as exc:
            console.print(f"[red]SSE 连接失败: {exc}[/]")
            sys.exit(1)


@main.command("worker-run")
@click.argument("description")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--difficulty", default="medium", show_default=True, type=click.Choice(["trivial", "medium", "complex"]))
@click.option("--writable", default="", help="可写路径，逗号分隔；留空=全项目")
@click.option("--readable", default="", help="可读路径，逗号分隔；留空=全项目")
@click.option("--watch", "-w", is_flag=True, help="SSE 跟踪 Worker 进度")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True, help="Swarm API 地址")
def worker_run(
    description: str,
    project: str,
    difficulty: str,
    writable: str,
    readable: str,
    watch: bool,
    api_url: str,
):
    """Phase 0 — 单 Worker 直跑（不经 Brain）"""
    w = _parse_scope_csv(writable)
    r = _parse_scope_csv(readable)
    scope_note = ""
    if w or r:
        scope_note = f"\nScope: writable={w or '全项目'} readable={r or '全项目'}"
    console.print(Panel(
        f"[bold blue]🔧 Worker 直跑[/]\n\n项目: {project}\n描述: {description}\n难度: {difficulty}{scope_note}"
    ))
    asyncio.run(_worker_run_via_api(description, project, difficulty, w, r, watch, api_url.rstrip("/")))


def _parse_scope_csv(raw: str) -> list[str] | None:
    if not raw or not raw.strip():
        return None
    paths = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    return paths or None


async def _worker_run_via_api(
    description: str,
    project: str,
    difficulty: str,
    writable: list[str] | None,
    readable: list[str] | None,
    watch: bool,
    api_url: str,
) -> None:
    payload: dict = {"description": description, "difficulty": difficulty}
    if writable:
        payload["writable"] = writable
    if readable:
        payload["readable"] = readable
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), headers=_auth_headers()) as client:
        try:
            resp = await client.post(
                f"{api_url}/api/projects/{project}/worker/run",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            console.print(f"[red]启动失败 ({exc.response.status_code}): {exc.response.text}[/]")
            sys.exit(1)
        except httpx.RequestError as exc:
            console.print(f"[red]无法连接 API: {exc}[/]")
            sys.exit(1)

        run_id = resp.json().get("run_id")
        if not run_id:
            console.print("[red]API 未返回 run_id[/]")
            sys.exit(1)
        console.print(f"[green]✅ Worker 已启动[/] run_id={run_id}")
        if not watch:
            console.print("[dim]使用 --watch 跟踪进度[/dim]")
            return

        stream_url = f"{api_url}/api/worker/{run_id}/stream"
        async with httpx.AsyncClient(timeout=None, headers=_auth_headers()) as stream_client:
            async with stream_client.stream("GET", stream_url) as stream:
                stream.raise_for_status()
                event_type = "progress"
                async for raw_line in stream.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        event_type = line.split(":", 1)[1].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line.split(":", 1)[1].strip()
                    if not data_str:
                        continue
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        console.print(f"[dim]{data_str}[/]")
                        continue
                    msg = data.get("message") or ""
                    step = data.get("step") or event_type
                    if msg:
                        console.print(f"[cyan]{step}[/] {msg}")
                    if step == "result" or event_type == "result":
                        result = data.get("result") or data
                        diff = result.get("diff") or ""
                        l1 = result.get("l1_passed")
                        console.print(Panel(
                            f"L1: {'通过' if l1 else '未通过'}\n"
                            f"摘要: {result.get('summary', '')}\n"
                            f"Diff 行数: {len(diff.splitlines()) if diff else 0}",
                            title="Worker 结果",
                        ))
                    if step in ("complete", "error"):
                        if step == "error":
                            sys.exit(1)
                        break


@main.group()
def task():
    """任务审核与 Diff 操作（经 API）"""
    pass


@task.command("approve")
@click.argument("task_id")
@click.option("--apply-diff", is_flag=True, help="通过时 git apply merged_diff")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_approve(task_id: str, apply_diff: bool, api_url: str):
    """审核通过任务"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(
            f"{api_url}/api/tasks/{task_id}/approve",
            json={"apply_diff": apply_diff},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        console.print(f"[green]✅ {data.get('message', '已通过')}[/]")
        if data.get("apply_diff"):
            console.print(f"[dim]apply: {data['apply_diff']}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败 ({exc.response.status_code}): {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]")
        sys.exit(1)


@task.command("revise")
@click.argument("task_id")
@click.argument("feedback")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_revise(task_id: str, feedback: str, api_url: str):
    """提交修订意见"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(
            f"{api_url}/api/tasks/{task_id}/revise",
            json={"feedback": feedback},
            timeout=30.0,
        )
        resp.raise_for_status()
        console.print(f"[green]✅ {resp.json().get('message', '已提交修订')}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@task.command("reject")
@click.argument("task_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_reject(task_id: str, api_url: str):
    """拒绝任务"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(f"{api_url}/api/tasks/{task_id}/reject", timeout=30.0)
        resp.raise_for_status()
        console.print(f"[green]✅ {resp.json().get('message', '已拒绝')}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@task.command("cancel")
@click.argument("task_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_cancel(task_id: str, api_url: str):
    """取消运行中或 orphaned 任务"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(f"{api_url}/api/tasks/{task_id}/cancel", timeout=30.0)
        resp.raise_for_status()
        console.print(f"[green]✅ {resp.json().get('message', '已取消')}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败 ({exc.response.status_code}): {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]")
        sys.exit(1)


@task.command("retry")
@click.argument("task_id")
@click.option("--auto-accept", is_flag=True, help="重跑时自动通过人工审核")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_retry(task_id: str, auto_accept: bool, api_url: str):
    """重跑失败/已取消/orphaned 任务"""
    api_url = api_url.rstrip("/")
    body = {"auto_accept": True} if auto_accept else None
    try:
        resp = _hpost(
            f"{api_url}/api/tasks/{task_id}/retry",
            json=body,
            timeout=30.0,
        )
        resp.raise_for_status()
        console.print(f"[green]✅ {resp.json().get('message', '已提交重跑')}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败 ({exc.response.status_code}): {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]")
        sys.exit(1)


@task.command("apply-diff")
@click.argument("task_id")
@click.option("--check-only", is_flag=True, help="仅 git apply --check")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_apply_diff(task_id: str, check_only: bool, api_url: str):
    """将任务 merged_diff 应用到项目工作区"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(
            f"{api_url}/api/tasks/{task_id}/apply-diff",
            json={"check_only": check_only},
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        console.print(f"[green]✅ {data.get('message') or ('校验通过' if check_only else '已应用')}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.command()
@click.option("--project", "-p", help="检查特定项目")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def status(project: str | None, api_url: str):
    """查看系统状态"""
    api_url = api_url.rstrip("/")
    table = Table(title="🐝 Swarm System Status")
    table.add_column("组件", style="cyan")
    table.add_column("状态", style="green")
    table.add_column("说明")

    try:
        resp = _hget(f"{api_url}/api/status", timeout=5.0)
        if resp.status_code == 200:
            data = resp.json()
            for name, info in (data.get("components") or {}).items():
                if isinstance(info, dict):
                    table.add_row(name, info.get("status", "?"), info.get("detail", ""))
                else:
                    table.add_row(name, str(info), "")
            table.add_row("API", "✅ 在线", api_url)
        else:
            table.add_row("API", "⚠️ 异常", f"HTTP {resp.status_code}")
    except httpx.RequestError:
        table.add_row("API", "❌ 离线", api_url)

    if project:
        try:
            resp = _hget(f"{api_url}/api/projects/{project}", timeout=5.0)
            if resp.status_code == 200:
                proj = resp.json().get("project") or resp.json()
                table.add_row(
                    f"项目 {project}",
                    proj.get("status", "?"),
                    proj.get("graph_status", ""),
                )
        except httpx.RequestError:
            pass

    console.print(table)


def _demo_enabled() -> bool:
    """C9：demo 直跑本地 Brain 完全绕过 API/RBAC/审计——生产默认禁用，须显式 env 开启。"""
    import os
    return os.environ.get("SWARM_DEMO_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


@main.command()
def demo():
    """运行演示任务（本地 Brain，无需 API）——【仅开发】，绕过 API/RBAC/审计。"""
    if not _demo_enabled():
        console.print(
            "[red]demo 已禁用[/]：本地 Brain 直跑会绕过 API/RBAC/审计，生产环境默认关闭。\n"
            "如确需在开发机运行，设 [bold]SWARM_DEMO_ENABLED=1[/] 后重试。"
        )
        raise SystemExit(2)
    console.print(Panel("[bold]🐝 Swarm Demo — 本地 Brain 模式（dev-only）[/]"))
    asyncio.run(_run_demo())


async def _run_demo():
    from swarm.brain.graph import compile_brain_graph
    from swarm.brain.state import BrainState

    graph = compile_brain_graph()

    console.print("[dim]Step 1: 提交模拟任务...[/dim]")
    initial_state: BrainState = {
        "task_id": "demo-001",
        "task_description": "给用户列表加排序功能",
        "project_id": "demo-project",
        "complexity": "medium",
        "knowledge_context": {},
        "plan": None,
        "plan_valid": None,
        "plan_retry_count": 0,
        "subtask_results": [],
        "dispatch_remaining": [],
        "failed_subtask_ids": [],
        "merged_diff": None,
        "l2_passed": None,
        "human_decision": None,
        "revision_feedback": None,
        "learned": False,
        "learn_summary": "",
    }

    config = {"configurable": {"thread_id": "demo-thread"}}

    try:
        result = await graph.ainvoke(initial_state, config=config)
        console.print("[green]✅ 状态机执行完成[/]")
        console.print(
            json.dumps(
                {k: v for k, v in result.items() if v is not None and v != [] and v != {}},
                indent=2,
                default=str,
                ensure_ascii=False,
            )
        )
    except Exception as e:
        console.print(f"[red]❌ 执行出错: {e}[/]")
        import traceback

        traceback.print_exc()


@main.group()
def profile():
    """L1 用户档案卡"""
    pass


@profile.command("show")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def profile_show(project: str, api_url: str):
    """查看当前用户 L1 画像"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/memories/profile", timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        console.print_json(json.dumps(data.get("profile_json") or data, indent=2, ensure_ascii=False))
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@profile.command("set")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--field", "-f", required=True, help="JSON 路径键，如 preferences.language")
@click.option("--value", "-v", required=True, help="字段值")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def profile_set(project: str, field: str, value: str, api_url: str):
    """更新 L1 画像字段（整字段覆盖）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/memories/profile", timeout=15.0)
        resp.raise_for_status()
        profile_json = resp.json().get("profile_json") or {}
        keys = field.split(".")
        node = profile_json
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        try:
            parsed_val = json.loads(value)
        except json.JSONDecodeError:
            parsed_val = value
        node[keys[-1]] = parsed_val
        put = _hput(
            f"{api_url}/api/projects/{project}/memories/profile",
            json={"profile_json": profile_json},
            timeout=15.0,
        )
        put.raise_for_status()
        console.print(f"[green]✅ 已更新 {field}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.group()
def errors():
    """L5 错题集"""
    pass


@errors.command("list")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def errors_list(project: str, api_url: str):
    """列出项目错题"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/memories/mistakes", timeout=15.0)
        resp.raise_for_status()
        mistakes = resp.json().get("mistakes") or []
        table = Table(title=f"错题集 — {project}")
        table.add_column("ID", style="dim")
        table.add_column("类型")
        table.add_column("描述")
        table.add_column("权重")
        for m in mistakes[:30]:
            table.add_row(
                str(m.get("id", "")),
                str(m.get("error_type", "")),
                (m.get("description") or "")[:60],
                str(m.get("decay_weight", "")),
            )
        console.print(table)
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@errors.command("dismiss")
@click.argument("mistake_id", type=int)
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def errors_dismiss(mistake_id: int, project: str, api_url: str):
    """标记错题为已修复/归档"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(
            f"{api_url}/api/projects/{project}/memories/mistakes/{mistake_id}/dismiss",
            timeout=15.0,
        )
        resp.raise_for_status()
        console.print(f"[green]✅ 错题 #{mistake_id} 已 dismiss[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.group()
def patterns():
    """L6 成功模式集"""
    pass


@patterns.command("list")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def patterns_list(project: str, api_url: str):
    """列出成功模式"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/memories/successes", timeout=15.0)
        resp.raise_for_status()
        items = resp.json().get("successes") or []
        table = Table(title=f"成功模式 — {project}")
        table.add_column("ID", style="dim")
        table.add_column("名称")
        table.add_column("描述")
        table.add_column("重用")
        for s in items[:30]:
            table.add_row(
                str(s.get("id", "")),
                (s.get("pattern_name") or "")[:40],
                (s.get("description") or "")[:50],
                str(s.get("reuse_count", 0)),
            )
        console.print(table)
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.group()
def sandbox():
    """远程沙箱管理（E2B/CubeSandbox）"""
    pass


@sandbox.command("list")
@click.option("--project", "-p", default=None, help="按项目 ID 过滤")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def sandbox_list(project: str | None, api_url: str):
    """列出活跃沙箱"""
    api_url = api_url.rstrip("/")
    params = {"project_id": project} if project else {}
    try:
        resp = _hget(f"{api_url}/api/sandbox/status", params=params, headers=_auth_headers(), timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        sandboxes = data.get("sandboxes") or []
        table = Table(title=f"活跃沙箱 — {data.get('active_count', len(sandboxes))} 个")
        table.add_column("ID", style="cyan")
        table.add_column("状态", style="green")
        table.add_column("模板")
        table.add_column("CPU/内存")
        table.add_column("项目")
        table.add_column("来源", style="dim")
        for sb in sandboxes:
            cpu = sb.get("cpu_count")
            mem = sb.get("memory_mb")
            res = f"{cpu or '?'}C/{mem or '?'}M" if (cpu or mem) else "-"
            table.add_row(
                str(sb.get("id", ""))[:24],
                str(sb.get("status", "?")),
                str(sb.get("template_id", "-"))[:20],
                res,
                str(sb.get("project_id") or "-")[:16],
                str(sb.get("source") or "-"),
            )
        console.print(table)
        cfg = data.get("config") or {}
        if cfg:
            console.print(
                f"[dim]server={cfg.get('api_url', '-')} | template={cfg.get('default_template', '-')} | "
                f"worker启用={cfg.get('use_for_worker')}[/]"
            )
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API ({api_url}): {exc}[/]")
        sys.exit(1)


@sandbox.command("create")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--template", "-t", default=None, help="模板 ID（留空用默认）")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def sandbox_create(project: str, template: str | None, api_url: str):
    """创建新沙箱"""
    api_url = api_url.rstrip("/")
    payload: dict = {"project_id": project}
    if template:
        payload["template_id"] = template
    try:
        resp = _hpost(f"{api_url}/api/sandbox/create", json=payload, headers=_auth_headers(), timeout=60.0)
        resp.raise_for_status()
        data = resp.json()
        console.print(f"[green]✅ 沙箱已创建: {data.get('sandbox_id') or data.get('id') or data}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@sandbox.command("destroy")
@click.argument("sandbox_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def sandbox_destroy(sandbox_id: str, api_url: str):
    """销毁指定沙箱"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hdelete(f"{api_url}/api/sandbox/{sandbox_id}", headers=_auth_headers(), timeout=30.0)
        resp.raise_for_status()
        console.print(f"[green]✅ 沙箱 {sandbox_id} 已销毁[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.group()
def config():
    """配置与模型路由"""
    pass


@config.command("show")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def config_show(api_url: str):
    """查看当前配置（API Key 已脱敏）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/config", headers=_auth_headers(), timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        console.print_json(json.dumps(data.get("flat") or data.get("config") or data, indent=2, ensure_ascii=False))
        ls = data.get("langsmith") or {}
        if ls:
            console.print(f"[dim]LangSmith: {ls}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API ({api_url}): {exc}[/]")
        sys.exit(1)


@config.command("models")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def config_models(api_url: str):
    """列出可用模型（SiliconFlow + 本地）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/models", headers=_auth_headers(), timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        for provider in ("siliconflow", "local"):
            models = data.get(provider) or []
            err = data.get(f"{provider}_error")
            table = Table(title=f"{provider} — {len(models)} 个模型")
            table.add_column("模型 ID", style="cyan")
            for m in models:
                table.add_row(str(m))
            console.print(table)
            if err:
                console.print(f"[yellow]{provider} 拉取错误: {err}[/]")
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@config.command("routing")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def config_routing(api_url: str):
    """查看 Worker 子任务模型路由表（trivial/medium/complex/multimodal）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/routing", headers=_auth_headers(), timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        routing = data.get("routing") or data
        table = Table(title="Worker 模型路由")
        table.add_column("难度档", style="cyan")
        table.add_column("模型")
        if isinstance(routing, dict):
            for tier, model in routing.items():
                table.add_row(str(tier), str(model))
        console.print(table)
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.group()
def kb():
    """知识库（Knowledge Base）"""
    pass


@kb.command("overview")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def kb_overview(project: str, api_url: str):
    """项目知识库概览（预处理结果 + 索引统计）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/knowledge/overview", headers=_auth_headers(), timeout=20.0)
        resp.raise_for_status()
        data = resp.json()
        table = Table(title=f"知识库概览 — {project}")
        table.add_column("指标", style="cyan")
        table.add_column("值", style="green")
        table.add_row("状态", str(data.get("status", "-")))
        table.add_row("图谱状态", str(data.get("graph_status", "-")))
        table.add_row("文件数", str(data.get("file_count", 0)))
        table.add_row("符号数", str(data.get("symbol_count", data.get("project_symbol_count", 0))))
        table.add_row("规范数 (norms)", str(data.get("norms_count", 0)))
        console.print(table)
        desc = data.get("description")
        if desc:
            console.print(Panel(desc[:500], title="项目摘要", border_style="dim"))
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API ({api_url}): {exc}[/]")
        sys.exit(1)


@kb.command("norms")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def kb_norms(project: str, api_url: str):
    """列出项目规范（Layer C norms）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/knowledge/norms", headers=_auth_headers(), timeout=15.0)
        resp.raise_for_status()
        norms = resp.json().get("norms") or []
        table = Table(title=f"项目规范 — {project}")
        table.add_column("ID", style="dim")
        table.add_column("类别")
        table.add_column("内容")
        for nm in norms[:40]:
            table.add_row(
                str(nm.get("id", "")),
                str(nm.get("category", nm.get("norm_type", ""))),
                (nm.get("content") or nm.get("rule") or "")[:70],
            )
        console.print(table)
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@kb.command("symbols")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--query", "-q", default="", help="符号名模糊查询")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def kb_symbols(project: str, query: str, api_url: str):
    """查询代码符号索引（Layer A）"""
    api_url = api_url.rstrip("/")
    params = {"q": query} if query else {}
    try:
        resp = _hget(
            f"{api_url}/api/projects/{project}/knowledge/symbols", params=params, headers=_auth_headers(), timeout=15.0
        )
        resp.raise_for_status()
        symbols = resp.json().get("symbols") or []
        table = Table(title=f"符号索引 — {project} ({len(symbols)} 个)")
        table.add_column("符号", style="cyan")
        table.add_column("类型")
        table.add_column("文件", style="dim")
        for s in symbols[:40]:
            table.add_row(
                str(s.get("name", "")),
                str(s.get("kind", s.get("type", ""))),
                str(s.get("file", s.get("path", "")))[:50],
            )
        console.print(table)
    except httpx.HTTPStatusError as exc:
        console.print(f"[red]失败: {exc.response.text}[/]")
        sys.exit(1)


@main.command()
def check():
    """检查所有模块导入和依赖"""
    errors = []

    checks = [
        ("swarm.types", "核心类型"),
        ("swarm.config", "配置系统"),
        ("swarm.models", "模型路由"),
        ("swarm.tools", "Tool 框架"),
        ("swarm.worker", "Worker Agent"),
        ("swarm.brain", "Brain 状态机"),
        ("swarm.knowledge", "知识库"),
        ("swarm.memory", "记忆系统"),
    ]

    for module_name, desc in checks:
        try:
            __import__(module_name)
            console.print(f"  ✅ {module_name} ({desc})")
        except Exception as e:
            console.print(f"  ❌ {module_name} ({desc}): {e}")
            errors.append((module_name, str(e)))

    if errors:
        console.print(f"\n[red]{len(errors)} 个模块导入失败[/]")
        sys.exit(1)
    else:
        console.print(f"\n[green]✅ 全部 {len(checks)} 个模块导入成功[/]")


# ══════════════════════════════════════════════════════════════════════════
# round22 CLI 补全：项目生命周期 + 预处理 + 任务列表 + 用户/成员(RBAC) + KB 检索
# 全部走 HTTP + 统一注入 token（_hget/_hpost/... 已带 _auth_headers）。
# ══════════════════════════════════════════════════════════════════════════

def _print_err(resp) -> None:
    """统一打印 API 错误（含 401 提示已在 _http 里给）。"""
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:  # noqa: BLE001
        detail = resp.text
    console.print(f"[red]✗ HTTP {resp.status_code}: {detail}[/]")


# ─── project：项目生命周期 ───────────────────────────────
@main.group()
def project():
    """项目管理：list / create / show / delete / stats"""


@project.command("list")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def project_list(api_url: str):
    """列出当前用户可见的项目"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    projects = resp.json().get("projects", [])
    if not projects:
        console.print("[dim]（无项目）[/]"); return
    table = Table(title="🐝 项目列表")
    table.add_column("ID", style="cyan"); table.add_column("名称")
    table.add_column("状态", style="green"); table.add_column("路径", style="dim")
    for p in projects:
        table.add_row(str(p.get("id", ""))[:12], p.get("name", ""),
                      p.get("status", "?"), p.get("path", ""))
    console.print(table)


@project.command("create")
@click.argument("name")
@click.option("--path", default="", help="项目根目录绝对路径；留空+--greenfield 自动在 workspace 建")
@click.option("--description", "-d", default="", help="项目描述")
@click.option("--greenfield", is_flag=True, help="从零创建空项目")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def project_create(name: str, path: str, description: str, greenfield: bool, api_url: str):
    """创建项目"""
    api_url = api_url.rstrip("/")
    body = {"name": name, "path": path, "description": description, "greenfield": greenfield}
    try:
        resp = _hpost(f"{api_url}/api/projects", json=body, timeout=30.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code not in (200, 201):
        _print_err(resp); sys.exit(1)
    proj = resp.json().get("project") or resp.json()
    console.print(f"[green]✅ 项目已创建[/] id={proj.get('id')} name={proj.get('name')}")


@project.command("show")
@click.argument("project_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def project_show(project_id: str, api_url: str):
    """查看项目详情"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project_id}", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    proj = resp.json().get("project") or resp.json()
    console.print(json.dumps(proj, indent=2, ensure_ascii=False, default=str))


@project.command("delete")
@click.argument("project_id")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def project_delete(project_id: str, yes: bool, api_url: str):
    """删除项目"""
    api_url = api_url.rstrip("/")
    if not yes and not click.confirm(f"确认删除项目 {project_id}？"):
        return
    try:
        resp = _hdelete(f"{api_url}/api/projects/{project_id}", timeout=30.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    console.print(f"[green]✅ 项目 {project_id} 已删除[/]")


@project.command("stats")
@click.argument("project_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def project_stats(project_id: str, api_url: str):
    """查看项目统计（任务数/成功率/PARTIAL 等）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project_id}/stats", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    console.print(json.dumps(resp.json(), indent=2, ensure_ascii=False, default=str))


# ─── preprocess：项目预处理（KB 就绪）────────────────────
@main.group()
def preprocess():
    """项目预处理（构建知识库）：run / status"""


@preprocess.command("run")
@click.argument("project_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def preprocess_run(project_id: str, api_url: str):
    """触发/重跑项目预处理"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hpost(f"{api_url}/api/projects/{project_id}/preprocess", timeout=30.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code not in (200, 202):
        _print_err(resp); sys.exit(1)
    console.print(f"[green]✅ 已触发预处理[/] project={project_id}（用 `swarm preprocess status {project_id}` 跟踪）")


@preprocess.command("status")
@click.argument("project_id")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def preprocess_status(project_id: str, api_url: str):
    """查看预处理进度/状态"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project_id}/preprocess/status", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    console.print(json.dumps(resp.json(), indent=2, ensure_ascii=False, default=str))


# ─── task list（补全任务生命周期）──────────────────────────
@task.command("list")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def task_list(project: str, api_url: str):
    """列出项目下的任务"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/tasks", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    tasks = resp.json().get("tasks", resp.json() if isinstance(resp.json(), list) else [])
    if not tasks:
        console.print("[dim]（无任务）[/]"); return
    table = Table(title=f"🐝 任务列表 · {project[:12]}")
    table.add_column("ID", style="cyan"); table.add_column("状态", style="green")
    table.add_column("描述", style="dim", max_width=50)
    for t in tasks:
        table.add_row(str(t.get("id", ""))[:12], t.get("status", "?"),
                      (t.get("description") or "")[:50])
    console.print(table)


# ─── user + member：用户与项目成员（RBAC 管理）────────────
@main.group()
def user():
    """用户管理（需 config:write）：list"""


@user.command("list")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def user_list(api_url: str):
    """列出全部用户（需管理员）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/users", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    users = resp.json().get("users", [])
    table = Table(title="🐝 用户")
    table.add_column("ID", style="cyan"); table.add_column("用户名")
    table.add_column("全局角色", style="green")
    for u in users:
        table.add_row(str(u.get("id", ""))[:12], u.get("username", ""),
                      u.get("global_role", "?"))
    console.print(table)


@main.group()
def member():
    """项目成员/RBAC：list / add / remove"""


@member.command("list")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def member_list(project: str, api_url: str):
    """列出项目成员及角色"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hget(f"{api_url}/api/projects/{project}/members", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    members = resp.json().get("members", [])
    table = Table(title=f"🐝 成员 · {project[:12]}")
    table.add_column("用户 ID", style="cyan"); table.add_column("角色", style="green")
    for m in members:
        table.add_row(str(m.get("user_id", m.get("id", ""))), m.get("role", "?"))
    console.print(table)


@member.command("add")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--user-id", "-u", required=True, help="用户 ID")
@click.option("--role", "-r", default="developer", show_default=True,
              help="角色：owner/developer/viewer 等")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def member_add(project: str, user_id: str, role: str, api_url: str):
    """添加/设置项目成员角色（需 member:manage）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hput(f"{api_url}/api/projects/{project}/members",
                     json={"user_id": user_id, "role": role}, timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    console.print(f"[green]✅ 成员已设置[/] {user_id} → {role} @ {project[:12]}")


@member.command("remove")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--user-id", "-u", required=True, help="用户 ID")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def member_remove(project: str, user_id: str, api_url: str):
    """移除项目成员（需 member:manage）"""
    api_url = api_url.rstrip("/")
    try:
        resp = _hdelete(f"{api_url}/api/projects/{project}/members/{user_id}", timeout=15.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    console.print(f"[green]✅ 成员已移除[/] {user_id} @ {project[:12]}")


# ─── kb retrieve（知识库检索预览）──────────────────────────
@kb.command("retrieve")
@click.argument("query")
@click.option("--project", "-p", required=True, help="项目 ID")
@click.option("--top-k", type=int, default=None, help="单层上限（可选）")
@click.option("--api-url", default=DEFAULT_API_URL, show_default=True)
def kb_retrieve(query: str, project: str, top_k: int | None, api_url: str):
    """按任务描述检索知识库（预览 Brain 将注入的上下文）"""
    api_url = api_url.rstrip("/")
    body: dict = {"query": query}
    if top_k is not None:
        body["top_k"] = top_k
    try:
        resp = _hpost(f"{api_url}/api/projects/{project}/knowledge/retrieve",
                      json=body, timeout=30.0)
    except httpx.RequestError as exc:
        console.print(f"[red]无法连接 API: {exc}[/]"); sys.exit(1)
    if resp.status_code != 200:
        _print_err(resp); sys.exit(1)
    console.print(json.dumps(resp.json(), indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
