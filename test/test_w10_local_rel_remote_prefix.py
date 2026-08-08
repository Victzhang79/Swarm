"""#29-5 W-10：_local_rel 对 /workspace 绝对路径的三条臂——剥远端根 / 本地根 / 显式拒绝。

修复前（29 号文 W-10，已实测）：`_local_rel('/workspace/src/main/java/A.java') -> 'A.java'`
——认不出的绝对路径静默回退裸 basename。链条：scope `writable=["src/main/java/A.java"]`
时 `require_writable("/workspace/src/main/java/A.java")` 因 `_path_scope_match` 规则4
（多段 scope 容忍根前缀、尾段命中）**通过** → `_local_rel` 对本地根 `relative_to` 必
ValueError → 回退 `p.name` → 落点变 `/workspace/A.java`，回执「✅ 成功」而目标文件
一字节未改 → pull-back 拉不到变更 → diff 缺文件 → 判空产出。agent 在沙箱里看到的
一切路径（编译报错、find 输出）都带 sandbox_remote_workdir 前缀，复制粘贴是常态。

每条断言的判据：把 `_local_rel` 改回 `except ValueError: return p.name`，对应测试必须红。
"""
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import swarm.tools.file_tools as ft
from swarm.tools.build_tools import clear_sandbox_context, set_sandbox_context
from swarm.tools.file_tools import PathResolutionError, _local_rel
from swarm.tools.paths import set_workspace_root
from swarm.tools.scope_guard import clear_scope, set_scope
from swarm.types import FileScope


class _MockFiles:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def read(self, path: str, format: str = "bytes") -> bytes:
        if path not in self.store:
            raise FileNotFoundError(path)
        return self.store[path]

    def write(self, path: str, data: bytes) -> None:
        self.store[path] = data if isinstance(data, bytes) else data.encode("utf-8")


@pytest.fixture()
def sandbox_env(tmp_path, monkeypatch):
    """真接线 mock 沙箱（同 test_file_tools_sandbox 风格）：ContextVar 工作根 +
    mock 沙箱上下文 + scope。yield (files_store,) 后清理全部全局态。"""
    monkeypatch.setenv("SWARM_WORKSPACE_ROOT", str(tmp_path))
    set_workspace_root(str(tmp_path))
    files = _MockFiles()
    sandbox = MagicMock()
    sandbox.sandbox_id = "sbx-w10"
    sandbox.files = files
    manager = MagicMock()
    set_sandbox_context(sandbox, manager)
    try:
        yield files
    finally:
        clear_sandbox_context()
        clear_scope()
        # ContextVar 不会随 monkeypatch 回滚——不清则后续测试的 workspace_root()
        # 解析到已删除的 tmp 目录（实测污染 test_local_fallback_without_sandbox）。
        set_workspace_root(None)


# ── _local_rel 三条臂（纯函数面）──

def test_remote_root_prefix_stripped():
    """沙箱式绝对路径剥远端根前缀——修复的核心臂。

    旧实现回退 p.name 返 'A.java' → 本测试红。
    """
    assert _local_rel("/workspace/src/main/java/A.java") == "src/main/java/A.java"


def test_foreign_absolute_raises_not_basename():
    """认不出的绝对路径显式拒绝 + WARNING——绝不回退 basename（fail-closed）。"""
    with pytest.raises(PathResolutionError):
        _local_rel("/etc/passwd")


def test_foreign_absolute_logs_warning(caplog):
    """降级路径至少一次 WARNING（硬检查④：缺席/拒绝必须可观测）。"""
    with caplog.at_level(logging.WARNING, logger="swarm.tools.file_tools"):
        with pytest.raises(PathResolutionError):
            _local_rel("/etc/passwd")
    assert any("无法定位" in r.message for r in caplog.records)


def test_dotdot_escape_cannot_be_stripped():
    """`/workspace/../etc/x`：normpath 后逸出远端根 → 拒绝（不剥出 '../etc/x'）。"""
    with pytest.raises(PathResolutionError):
        _local_rel("/workspace/../etc/x")


def test_relative_passthrough():
    assert _local_rel("src/main/java/A.java") == "src/main/java/A.java"


def test_local_root_absolute_still_stripped(sandbox_env):
    """本地 workspace 根下的绝对路径仍剥本地根（既有行为不回归）。"""
    import os
    local_abs = os.path.join(os.environ["SWARM_WORKSPACE_ROOT"], "src/a.py")
    assert _local_rel(local_abs) == "src/a.py"


# ── 端到端：write_file/patch_file/read_file 经沙箱路由的落点与拒绝 ──

def test_write_file_sandbox_style_absolute_lands_on_right_file(sandbox_env):
    """全链复现：scope 尾段命中放行 /workspace 前缀路径 → 写入【正确的】远端文件。

    旧行为：落点 '/workspace/A.java' 且回执 ✅（目标一字节未改）→ 本测试红。
    """
    files = sandbox_env
    set_scope(FileScope(writable=["src/main/java/A.java"]))
    from swarm.tools.file_tools import write_file

    out = write_file.invoke({"path": "/workspace/src/main/java/A.java", "content": "x\n"})
    assert "✅" in out
    assert files.store.get("/workspace/src/main/java/A.java") == b"x\n"
    assert "/workspace/A.java" not in files.store


def test_write_file_foreign_absolute_refused_not_misplaced(sandbox_env):
    """fail-closed：认不出的绝对路径显式拒绝，绝不写到 /workspace/<basename>。"""
    files = sandbox_env
    set_scope(FileScope(allow_any=True))
    from swarm.tools.file_tools import write_file

    out = write_file.invoke({"path": "/etc/evil.py", "content": "x\n"})
    assert "❌" in out and "无法定位" in out
    assert not files.store, f"拒绝后不得有任何落盘: {files.store}"


def test_patch_file_foreign_absolute_refused(sandbox_env):
    set_scope(FileScope(allow_any=True))
    from swarm.tools.file_tools import patch_file

    out = patch_file.invoke({
        "path": "/tmp/outside/target.py", "old_string": "a", "new_string": "b"})
    assert "❌" in out and "无法定位" in out


def test_read_file_foreign_absolute_refused(sandbox_env):
    set_scope(FileScope(allow_any=True))
    from swarm.tools.file_tools import read_file

    out = read_file.invoke({"path": "/etc/passwd"})
    assert "❌" in out and "无法定位" in out


def test_write_file_relative_path_still_works(sandbox_env):
    """相对路径主通道不回归。"""
    files = sandbox_env
    set_scope(FileScope(writable=["hello.py"]))
    from swarm.tools.file_tools import write_file

    out = write_file.invoke({"path": "hello.py", "content": "y\n"})
    assert "✅" in out
    assert files.store.get("/workspace/hello.py") == b"y\n"


# ── 双复核整改锁 ──

@pytest.mark.parametrize("root,given,expect", [
    ("/work", "/work/src/A.java", "src/A.java"),      # 自定义远端根
    ("/", "/src/A.java", "src/A.java"),               # 根目录远端根（rstrip 削空回归锁）
    ("/workspace/", "/workspace/src/A.java", "src/A.java"),  # 尾斜杠归一
])
def test_custom_remote_roots(monkeypatch, root, given, expect):
    """自定义/根目录/尾斜杠远端根都必须正确剥离（reviewer MED：rstrip('/') 把 '/'
    削成空串 ⇒ 剥离臂静默跳过 ⇒ 合法路径被冤拒）。"""
    monkeypatch.setattr(ft, "_remote_workdir", lambda: root)
    assert _local_rel(given) == expect


def test_refusal_carries_machine_key(sandbox_env):
    """拒绝回执带稳定机读键 [PATH_RESOLUTION_ERROR]（hunter MED：上游分类靠子串
    匹配自然语言太脆弱——机读键让同类拒绝可识别可统计）。"""
    set_scope(FileScope(allow_any=True))
    from swarm.tools.file_tools import write_file

    out = write_file.invoke({"path": "/etc/evil.py", "content": "x\n"})
    assert out.startswith("[PATH_RESOLUTION_ERROR]")


def test_remote_workdir_fallback_warns_once(monkeypatch, caplog):
    """配置读取失败的默认值回退必须可观测（hunter HIGH：静默回退 ⇒ 自定义远端根
    部署里合法路径被冤拒而运维零线索）。"""
    monkeypatch.setattr(ft, "_REMOTE_WORKDIR_WARNED", False)
    import swarm.config.settings as settings

    def _boom():
        raise ImportError("boom")

    monkeypatch.setattr(settings, "get_config", _boom)
    with caplog.at_level(logging.WARNING, logger="swarm.tools.file_tools"):
        assert ft._remote_workdir() == "/workspace"
        assert ft._remote_workdir() == "/workspace"
    warns = [r for r in caplog.records if "sandbox_remote_workdir" in r.message]
    assert len(warns) == 1, f"回退 WARNING 应每进程只响一次: {len(warns)}"
