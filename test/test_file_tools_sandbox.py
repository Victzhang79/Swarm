#!/usr/bin/env python3
"""file_tools sandbox-first 路由单元测试（mock 沙箱，无需真实 CubeSandbox）"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class _MockFiles:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    def read(self, path: str, format: str = "bytes") -> bytes:
        if path not in self.store:
            raise FileNotFoundError(path)
        return self.store[path]

    def write(self, path: str, data: bytes) -> None:
        self.store[path] = data if isinstance(data, bytes) else data.encode("utf-8")

    def list(self, path: str):
        entries = []
        prefix = path.rstrip("/") + "/"
        seen_dirs: set[str] = set()
        for p in self.store:
            if not p.startswith(prefix) and p != path.rstrip("/"):
                continue
            rest = p[len(prefix) :] if p.startswith(prefix) else ""
            if not rest:
                continue
            part = rest.split("/")[0]
            full = f"{path.rstrip('/')}/{part}"
            if "/" in rest:
                if full not in seen_dirs:
                    seen_dirs.add(full)
                    entries.append(_Entry(part, full, is_dir=True))
            else:
                entries.append(_Entry(part, p, is_dir=False))
        return entries


class _Entry:
    def __init__(self, name: str, path: str, is_dir: bool):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.type = "dir" if is_dir else "file"
        self.size = 0


def _setup_mock_sandbox(tmp_path: Path):
    import os

    from swarm.tools.build_tools import clear_sandbox_context, set_sandbox_context
    from swarm.tools.scope_guard import set_scope
    from swarm.types import FileScope

    os.environ["SWARM_WORKSPACE_ROOT"] = str(tmp_path)

    files = _MockFiles()
    files.write("/workspace/hello.py", b"line1\nline2\nline3\n")

    sandbox = MagicMock()
    sandbox.sandbox_id = "mock-sbx-001"
    sandbox.files = files

    manager = MagicMock()
    manager.list_files = lambda sid, path="/": [
        {"name": e.name, "path": e.path, "is_dir": e.is_dir, "size": 0}
        for e in files.list(path)
    ]

    scope = FileScope(writable=["hello.py"], readable=["hello.py", ".", ""])
    set_scope(scope)
    set_sandbox_context(sandbox, manager)
    return sandbox, manager, files, clear_sandbox_context


def test_read_file_sandbox():
    tmp = Path("/tmp/swarm_file_tools_test")
    tmp.mkdir(exist_ok=True)
    _, _, files, clear = _setup_mock_sandbox(tmp)
    try:
        from swarm.tools.file_tools import read_file

        out = read_file.invoke({"path": "hello.py", "start_line": 1, "end_line": 2})
        assert "1|line1" in out
        assert "2|line2" in out
        assert "3|" not in out
        print("  ✅ read_file 沙箱路由")
    finally:
        clear()


def test_write_and_patch_file_sandbox():
    tmp = Path("/tmp/swarm_file_tools_test")
    tmp.mkdir(exist_ok=True)
    sandbox, manager, files, clear = _setup_mock_sandbox(tmp)
    try:
        from swarm.tools.file_tools import patch_file, write_file

        w = write_file.invoke({"path": "hello.py", "content": "alpha\nbeta\n"})
        assert "✅" in w
        assert files.store["/workspace/hello.py"] == b"alpha\nbeta\n"

        p = patch_file.invoke({
            "path": "hello.py",
            "old_string": "beta",
            "new_string": "gamma",
        })
        assert "✅" in p
        assert b"gamma" in files.store["/workspace/hello.py"]
        print("  ✅ write_file / patch_file 沙箱路由")
    finally:
        clear()


def test_search_in_file_sandbox():
    tmp = Path("/tmp/swarm_file_tools_test")
    tmp.mkdir(exist_ok=True)
    _, _, files, clear = _setup_mock_sandbox(tmp)
    try:
        from swarm.tools.file_tools import search_in_file

        files.write("/workspace/hello.py", b"findme here\nother\n")
        out = search_in_file.invoke({"pattern": "findme", "path": ".", "file_glob": "*.py"})
        assert "findme" in out
        assert "hello.py" in out
        print("  ✅ search_in_file 沙箱路由")
    finally:
        clear()


def test_local_fallback_without_sandbox():
    import os
    import tempfile

    from swarm.tools.build_tools import clear_sandbox_context
    from swarm.tools.scope_guard import clear_scope, set_scope
    from swarm.types import FileScope

    clear_sandbox_context()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        os.environ["SWARM_WORKSPACE_ROOT"] = str(root)
        (root / "local.txt").write_text("local content\n", encoding="utf-8")
        scope = FileScope(writable=["local.txt"], readable=["local.txt"])
        set_scope(scope)
        try:
            from swarm.tools.file_tools import read_file

            out = read_file.invoke({"path": "local.txt"})
            assert "local content" in out
            print("  ✅ 无沙箱时本地 fallback")
        finally:
            clear_scope()


def main():
    print("\n🧪 file_tools sandbox-first 单元测试\n")
    tests = [
        test_read_file_sandbox,
        test_write_and_patch_file_sandbox,
        test_search_in_file_sandbox,
        test_local_fallback_without_sandbox,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
