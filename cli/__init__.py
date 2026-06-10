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


@click.group()
@click.version_option(version="0.1.0", prog_name="swarm")
def main():
    """🐝 Swarm — 蜂群 AI 编程智能体系统"""
    pass


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
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
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
            async with httpx.AsyncClient(timeout=None) as stream_client:
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
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
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
        async with httpx.AsyncClient(timeout=None) as stream_client:
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
        resp = httpx.post(
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
        resp = httpx.post(
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
        resp = httpx.post(f"{api_url}/api/tasks/{task_id}/reject", timeout=30.0)
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
        resp = httpx.post(f"{api_url}/api/tasks/{task_id}/cancel", timeout=30.0)
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
        resp = httpx.post(
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
        resp = httpx.post(
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
        resp = httpx.get(f"{api_url}/api/status", timeout=5.0)
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
            resp = httpx.get(f"{api_url}/api/projects/{project}", timeout=5.0)
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


@main.command()
def demo():
    """运行演示任务（本地 Brain，无需 API）"""
    console.print(Panel("[bold]🐝 Swarm Demo — 本地 Brain 模式[/]"))
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
        resp = httpx.get(f"{api_url}/api/projects/{project}/memories/profile", timeout=15.0)
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
        resp = httpx.get(f"{api_url}/api/projects/{project}/memories/profile", timeout=15.0)
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
        put = httpx.put(
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
        resp = httpx.get(f"{api_url}/api/projects/{project}/memories/mistakes", timeout=15.0)
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
        resp = httpx.post(
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
        resp = httpx.get(f"{api_url}/api/projects/{project}/memories/successes", timeout=15.0)
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


if __name__ == "__main__":
    main()
