"""文件操作 Tool 集 — read / write / patch / search

所有 Tool 自动检查 FileScope 权限，使用 ScopeGuard 的 context variable。
Worker 启用沙箱时，读写均在沙箱 /workspace 内进行（sandbox-first）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from langchain_core.tools import tool

from swarm.tools.scope_guard import require_readable, require_writable


# ──────────────────────────────────────────────
# 基础路径解析（相对于 workspace）
# ──────────────────────────────────────────────
def _resolve(path: str) -> Path:
    """将相对路径转为绝对路径"""
    from swarm.tools.paths import workspace_root

    p = Path(path)
    if not p.is_absolute():
        p = workspace_root() / p
    return p.resolve()


class WorkspaceEscapeError(PermissionError):
    """解析后的路径越出 workspace 边界（P0-SEC-07 防穿越复校）。"""


def _resolve_write(path: str) -> Path:
    """P0-SEC-07：写/改/删等【变更】操作的路径解析——在 .resolve() 跟随 symlink/`..` 后，
    复校结果仍位于 workspace_root 内（scope_guard 校验用原始字符串、落盘用 resolve 绝对路径，
    二者不一致时 symlink 可越界；此处补一道落盘前的边界复校，defense-in-depth）。
    """
    from swarm.tools.paths import workspace_root

    resolved = _resolve(path)
    root = Path(workspace_root()).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceEscapeError(
            f"路径越出 workspace 边界，拒绝写入: {path!r} → {resolved}"
        ) from exc
    return resolved


def _local_rel(path: str) -> str:
    """将路径转为 workspace 相对 posix 路径。"""
    p = Path(path)
    root = _resolve(".").resolve()
    if p.is_absolute():
        try:
            return p.resolve().relative_to(root).as_posix()
        except ValueError:
            return p.name
    return p.as_posix()


def _sandbox_active() -> bool:
    from swarm.config.settings import get_config
    from swarm.tools.build_tools import get_sandbox_context

    if not get_config().sandbox.sandbox_first:
        return False
    sandbox, manager = get_sandbox_context()
    return sandbox is not None and manager is not None


def _resolve_sandbox(path: str) -> str | None:
    """将 workspace 路径映射为沙箱内路径；无沙箱时返回 None。"""
    if not _sandbox_active():
        return None
    from swarm.config.settings import get_config
    from swarm.worker.sandbox import sandbox_path

    remote_root = get_config().sandbox.sandbox_remote_workdir
    return sandbox_path(_local_rel(path), remote_root)


def _read_sandbox_text(remote_path: str) -> str:
    from swarm.tools.build_tools import get_sandbox_context
    from swarm.worker.sandbox import read_file_from_sandbox

    sandbox, manager = get_sandbox_context()
    data = read_file_from_sandbox(sandbox, remote_path, manager=manager)
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


def _write_sandbox_text(remote_path: str, content: str) -> None:
    from swarm.tools.build_tools import get_sandbox_context
    from swarm.worker.sandbox import write_file_to_sandbox

    sandbox, manager = get_sandbox_context()
    write_file_to_sandbox(sandbox, remote_path, content, manager=manager)


def _format_numbered_lines(
    lines: list[str], start_line: int, end_line: int
) -> str:
    total = len(lines)
    s = max(1, start_line) - 1
    e = total if end_line == -1 else min(end_line, total)
    selected = lines[s:e]
    # 防 ReAct 上下文爆炸：单次 read 输出硬上限(行数 + 字节)。RuoYi 等企业项目
    # 有几千行的巨型文件(ExcelUtil/StringUtils 等)，无界返回会把工具结果累积进 message
    # 历史撑爆模型上下文。实测 Qwen3.5-122B(65536 窗口)改 877 行 StringUtils 时，450 行
    # 一次读≈6-9K token，多读两次+历史累积就超 65536 → 400 报错 → 子任务死循环。
    # 收紧到 150 行/8K 字符：单次读≈2-3K token，强制小模型用 start_line/end_line 局部读。
    _MAX_LINES = 150
    _MAX_CHARS = 8000
    truncated_note = ""
    if len(selected) > _MAX_LINES:
        shown_end = s + _MAX_LINES
        truncated_note = (
            f"\n... [已截断: 文件共 {total} 行，本次只显示 {s + 1}-{shown_end} 行。"
            f"用 read_file(path, start_line=N, end_line=M) 按行号分段读取后续内容] ..."
        )
        selected = selected[:_MAX_LINES]
    body = "".join(f"{i}|{line}" for i, line in enumerate(selected, start=s + 1))
    if len(body) > _MAX_CHARS:
        body = body[:_MAX_CHARS]
        truncated_note = (
            f"\n... [已截断: 输出超 {_MAX_CHARS} 字符。用 read_file 按更窄的行号区间读取] ..."
        )
    return body + truncated_note


@tool
def read_file(path: str, start_line: int = 1, end_line: int = -1) -> str:
    """读取文件内容。需要文件在可读范围内。

    Args:
        path: 文件路径（相对于 workspace 根目录或绝对路径均可）
        start_line: 起始行号（1-indexed），默认 1
        end_line: 结束行号，-1 表示读到文件末尾

    Returns:
        文件内容（带行号），或权限拒绝/文件不存在错误消息
    """
    err = require_readable(path)
    if err:
        return err

    remote = _resolve_sandbox(path)
    if remote is not None:
        try:
            text = _read_sandbox_text(remote)
            lines = text.splitlines(keepends=True)
            numbered = _format_numbered_lines(lines, start_line, end_line)
            return numbered or "(空文件)"
        except Exception as e:
            return f"❌ 沙箱读取失败：{e}"

    resolved = _resolve(path)
    if not resolved.exists():
        return f"❌ 文件不存在：{resolved}"

    try:
        lines = resolved.read_text(encoding="utf-8").splitlines(keepends=True)
        numbered = _format_numbered_lines(lines, start_line, end_line)
        return numbered or "(空文件)"
    except Exception as e:
        return f"❌ 读取失败：{e}"


@tool
def write_file(path: str, content: str) -> str:
    """写入文件，覆盖已有内容。需要文件在可写范围内。

    Args:
        path: 文件路径（相对于 workspace 根目录或绝对路径均可）
        content: 要写入的完整内容

    Returns:
        成功消息或权限拒绝/写入错误消息
    """
    err = require_writable(path)
    if err:
        return err

    remote = _resolve_sandbox(path)
    if remote is not None:
        try:
            _write_sandbox_text(remote, content)
            line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
            return f"✅ 已写入沙箱 {remote}（{line_count} 行）"
        except Exception as e:
            return f"❌ 沙箱写入失败：{e}"

    try:
        resolved = _resolve_write(path)  # P0-SEC-07：落盘前 workspace 边界复校
    except WorkspaceEscapeError as e:
        return f"❌ 拒绝写入（越界）：{e}"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        line_count = content.count("\n") + (0 if content.endswith("\n") else 1)
        return f"✅ 已写入 {resolved}（{line_count} 行）"
    except Exception as e:
        return f"❌ 写入失败：{e}"


@tool
def patch_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    """对文件进行精确的查找替换编辑。需要文件在可写范围内。

    Args:
        path: 文件路径
        old_string: 要查找的文本（必须精确匹配，包括缩进）
        new_string: 替换后的文本
        replace_all: 是否替换所有匹配项，默认仅替换第一处

    Returns:
        成功消息（含行号范围）或权限拒绝/未找到/多重匹配错误消息
    """
    err = require_writable(path)
    if err:
        return err

    remote = _resolve_sandbox(path)
    if remote is not None:
        try:
            text = _read_sandbox_text(remote)
        except Exception as e:
            return f"❌ 沙箱读取失败：{e}"
        display = remote
    else:
        try:
            resolved = _resolve_write(path)  # P0-SEC-07：变更操作落盘前 workspace 边界复校
        except WorkspaceEscapeError as e:
            return f"❌ 拒绝修改（越界）：{e}"
        if not resolved.exists():
            return f"❌ 文件不存在：{resolved}"
        try:
            text = resolved.read_text(encoding="utf-8")
        except Exception as e:
            return f"❌ 读取失败：{e}"
        display = str(resolved)

    try:
        count = text.count(old_string)

        if count == 0:
            return "❌ 未找到匹配文本，请检查 old_string 是否精确（包括缩进）"
        if count > 1 and not replace_all:
            return (
                f"❌ 找到 {count} 处匹配，请补充更多上下文使其唯一，"
                f"或设置 replace_all=True"
            )

        new_text = (
            text.replace(old_string, new_string)
            if replace_all
            else text.replace(old_string, new_string, 1)
        )

        if remote is not None:
            _write_sandbox_text(remote, new_text)
        else:
            resolved.write_text(new_text, encoding="utf-8")

        old_start = text[: text.index(old_string)].count("\n") + 1
        old_end = old_start + old_string.count("\n")
        return f"✅ 已替换 {display} 第 {old_start}-{old_end} 行"
    except Exception as e:
        return f"❌ 替换失败：{e}"


@tool
def search_in_file(
    pattern: str,
    path: str = ".",
    file_glob: str = "*",
    max_results: int = 50,
) -> str:
    """在文件中搜索正则表达式匹配。需要搜索路径在可读范围内。

    Args:
        pattern: 正则表达式搜索模式
        path: 搜索起始目录或文件路径，默认为 workspace 根目录
        file_glob: 文件名筛选模式，如 '*.py'，默认所有文件
        max_results: 最大返回结果数，默认 50

    Returns:
        匹配结果（含行号与上下文）或权限拒绝消息
    """
    err = require_readable(path)
    if err:
        return err

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"❌ 正则表达式无效：{e}"

    if _sandbox_active():
        return _search_in_sandbox(pattern, path, file_glob, max_results, regex)

    resolved = _resolve(path)
    if not resolved.exists():
        return f"❌ 路径不存在：{resolved}"

    results: list[str] = []
    files_to_search: list[Path] = []

    if resolved.is_file():
        files_to_search = [resolved]
    else:
        for root, _dirs, files in os.walk(resolved):
            root_path = Path(root)
            for fn in files:
                fp = root_path / fn
                from fnmatch import fnmatch

                if fnmatch(fn, file_glob):
                    files_to_search.append(fp)

    for fp in files_to_search:
        rel_path = str(fp)
        if require_readable(rel_path):
            continue

        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines, start=1):
            if regex.search(line):
                results.append(f"{fp}:{i}| {line.rstrip()}")
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break

    if not results:
        return f"未找到匹配 '{pattern}' 的内容"
    return "\n".join(results)


def _search_in_sandbox(
    pattern: str,
    path: str,
    file_glob: str,
    max_results: int,
    regex: re.Pattern[str],
) -> str:
    from fnmatch import fnmatch

    from swarm.tools.build_tools import get_sandbox_context
    from swarm.worker.sandbox import get_sandbox_manager

    sandbox, manager = get_sandbox_context()
    mgr = manager or get_sandbox_manager()
    remote_root = _resolve_sandbox(".") or "/workspace"
    start_remote = _resolve_sandbox(path) or remote_root

    results: list[str] = []

    def _walk(dir_path: str) -> None:
        nonlocal results
        if len(results) >= max_results:
            return
        try:
            entries = mgr.list_files(sandbox.sandbox_id, dir_path)
        except Exception:
            return
        for ent in entries:
            if len(results) >= max_results:
                return
            name = ent.get("name", "")
            full_path = ent.get("path") or f"{dir_path.rstrip('/')}/{name}"
            if not full_path.startswith("/"):
                full_path = f"/{full_path}"
            if ent.get("is_dir"):
                _walk(full_path)
                continue
            rel = full_path[len(remote_root) + 1 :] if full_path.startswith(remote_root + "/") else name
            if not fnmatch(Path(rel).name, file_glob):
                continue
            if require_readable(rel):
                continue
            try:
                text = _read_sandbox_text(full_path)
                lines = text.splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, start=1):
                if regex.search(line):
                    results.append(f"{rel}:{i}| {line.rstrip()}")
                    if len(results) >= max_results:
                        return

    if start_remote != remote_root and not start_remote.endswith("/"):
        try:
            text = _read_sandbox_text(start_remote)
            rel = _local_rel(path)
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    results.append(f"{rel}:{i}| {line.rstrip()}")
            return results and "\n".join(results) or f"未找到匹配 '{pattern}' 的内容"
        except Exception:
            return f"❌ 沙箱路径不存在：{start_remote}"

    _walk(start_remote if start_remote.endswith("/") or start_remote == remote_root else start_remote)
    if not results:
        return f"未找到匹配 '{pattern}' 的内容"
    return "\n".join(results)
