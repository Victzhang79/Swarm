"""round29 遗漏项#3：clean_upload 对 `git add -N` 占位文件写空上传（沙箱空 pom 根因）。

现场（task d37a52a3，7 个沙箱 × ~15min 空烧 23:45→01:44）：`Non-readable POM
/workspace/ruoyi-alarm/pom.xml: input contained no data`。三方 git 口径分叉链：
1. pull-back/diff 收集对 untracked 新文件跑 `git add -N`（intent-to-add）→ 文件进 index 占位；
2. clean_upload 的 `_git_tracked_set` 用 `git ls-files`（index 口径）→ 占位文件被判 tracked；
3. `_git_baseline_text` 用 `git show HEAD:rel`（HEAD 口径）→ HEAD 无此文件 → 按
   「新建文件 diff 基线=空串」语义返回 ""——该语义对 diff 基线正确，对【上传内容】是毒药；
4. `dst.write_text("")` → 0 字节 pom 进沙箱 → mvn "input contained no data"。

治本：clean_upload 的 tracked 判定改用【HEAD/base 树真实存在】口径（git ls-tree），
与"用 HEAD 版上传"的取用语义同源；add -N 占位自然归 untracked→磁盘拷贝正确路径。
reset 防脏等其它调用方不动（破坏范围最小化）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo_intent_add"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "mod.py").write_text("# HEAD clean\n")
    (repo / "empty_at_head.txt").write_text("")     # HEAD 里合法的空文件
    _git(repo, "add", "mod.py", "empty_at_head.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _run_sync(repo: Path, writable: list[str], rel_files: list[str], monkeypatch) -> dict:
    from swarm.worker.executor import WorkerExecutor

    # 跨 locale 确定性：修前 _git_baseline_text 按【英文 stderr 文案】判 HEAD 缺文件——
    # 中文 locale 下会意外走对路掩盖 bug。钉英文 locale 使本测试在任何机器上语义一致。
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")

    captured: dict = {}

    class _Mgr:
        def sync_files_to_sandbox(self, sandbox, local_root, rels, remote_root):
            captured["contents"] = {}
            for rel in rels:
                p = Path(local_root) / rel
                captured["contents"][rel] = p.read_text() if p.is_file() else None
            return {"uploaded": len(rels), "errors": [], "files": rels}

    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub.effective_scope = FileScope(writable=list(writable), readable=[], create_files=[])
    stub._sandbox = object()
    stub._sandbox_manager = _Mgr()
    stub._log = lambda m: None
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._scope_files = lambda: list(rel_files)
    stub._norm_rel = WorkerExecutor._norm_rel
    stub._git_baseline_text = WorkerExecutor._git_baseline_text.__get__(stub)
    stub._snapshot_scope_local = WorkerExecutor._snapshot_scope_local.__get__(stub)
    stub._sync_to_sandbox = WorkerExecutor._sync_to_sandbox.__get__(stub)

    import swarm.worker.executor as ex_mod
    monkeypatch.setattr(ex_mod, "get_config", lambda: SimpleNamespace(
        sandbox=SimpleNamespace(sandbox_remote_workdir="/workspace")
    ))
    asyncio.run(stub._sync_to_sandbox("bootstrap"))
    return captured["contents"]


def test_intent_to_add_file_uploads_disk_not_empty(tmp_path, monkeypatch):
    """核心复现：writable 新文件被 `git add -N` 占位（index 有、HEAD 无）→ 必须上传磁盘
    真实内容，绝不能被"HEAD 版"写成 0 字节（修前=空串上传，7 沙箱空 pom 现场）。"""
    repo = _make_repo(tmp_path)
    (repo / "newmod").mkdir()
    (repo / "newmod" / "pom.xml").write_text("<project>real content</project>\n")
    _git(repo, "add", "-N", "newmod/pom.xml")   # intent-to-add：进 index、HEAD 无

    contents = _run_sync(repo, writable=["newmod/pom.xml", "mod.py"],
                         rel_files=["newmod/pom.xml", "mod.py"], monkeypatch=monkeypatch)
    assert contents["newmod/pom.xml"] == "<project>real content</project>\n", (
        f"add -N 占位文件必须上传磁盘版，实际={contents['newmod/pom.xml']!r}"
    )


def test_head_tracked_file_still_gets_head_version(tmp_path, monkeypatch):
    """回归护栏：HEAD 里真实存在的 writable 文件仍用 HEAD 干净版（防脏叠加语义不回归）。"""
    repo = _make_repo(tmp_path)
    (repo / "mod.py").write_text("# HEAD clean\n# DIRTY overlay\n")

    contents = _run_sync(repo, writable=["mod.py"], rel_files=["mod.py"],
                         monkeypatch=monkeypatch)
    assert contents["mod.py"] == "# HEAD clean\n"


def test_head_empty_file_stays_head_empty(tmp_path, monkeypatch):
    """HEAD 里合法的 0 字节 tracked 文件：防脏叠加护栏仍生效（磁盘被污染也上传 HEAD 空版）。"""
    repo = _make_repo(tmp_path)
    (repo / "empty_at_head.txt").write_text("dirty overlay\n")

    contents = _run_sync(repo, writable=["empty_at_head.txt"],
                         rel_files=["empty_at_head.txt"], monkeypatch=monkeypatch)
    assert contents["empty_at_head.txt"] == ""


def test_reset_scope_survives_intent_to_add(tmp_path, monkeypatch):
    """sibling：workspace reset 的 checkout 列表混入 add -N 占位时，原先【整条 checkout 拒绝
    执行】=防脏叠加护栏全体失效（现场 pathspec 警告）。修后占位文件被 ls-tree 口径排除，
    真 tracked 文件照常恢复到 base。"""
    from swarm.types import FileScope
    from swarm.worker.executor import WorkerExecutor

    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    repo = _make_repo(tmp_path)
    (repo / "mod.py").write_text("# HEAD clean\n# DIRTY\n")   # 待恢复的脏 tracked
    (repo / "newmod").mkdir()
    (repo / "newmod" / "pom.xml").write_text("<project/>\n")
    _git(repo, "add", "-N", "newmod/pom.xml")

    stub = SimpleNamespace()
    stub.project_path = str(repo)
    stub.effective_scope = FileScope(writable=["mod.py", "newmod/pom.xml"], readable=[])
    stub._log = lambda m: None
    stub._writable_files = WorkerExecutor._writable_files.__get__(stub)
    stub._norm_rel = WorkerExecutor._norm_rel
    restored = WorkerExecutor._reset_scope_to_head.__get__(stub)()
    assert restored == 1, "真 tracked 文件必须恢复成功（占位混入不得使整条 checkout 失效）"
    assert (repo / "mod.py").read_text() == "# HEAD clean\n"
    assert (repo / "newmod" / "pom.xml").read_text() == "<project/>\n", "占位新文件不得被动"


def test_intent_to_add_cleaned_after_diff(tmp_path, monkeypatch):
    """hunter#1（入口对称）：_try_local_git_diff 的 add -N 占位在 diff 取得后必须对称清理，
    不得永久残留共享真仓 index（实测 81 条累积污染 git status/stash）。diff 内容不受影响。"""
    import types

    from swarm.worker.executor import WorkerExecutor

    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("LANG", "C")
    repo = _make_repo(tmp_path)
    (repo / "newmod").mkdir()
    (repo / "newmod" / "pom.xml").write_text("<project>new</project>\n")

    stub = types.SimpleNamespace(
        project_path=str(repo),
        effective_scope=types.SimpleNamespace(
            writable=["newmod/pom.xml"], create_files=[], delete_files=[]),
        _repaired_extra_paths=set(),
        _post_sync_contents={},
        _sandbox_manager=None,
        _log=lambda *a, **k: None,
    )
    diff = WorkerExecutor._try_local_git_diff(stub)
    assert diff and "newmod/pom.xml" in diff, "新文件必须被 diff 捕获（add -N 语义不回归）"
    ls = subprocess.run(["git", "ls-files", "--", "newmod/pom.xml"],
                        cwd=str(repo), capture_output=True, text=True)
    assert ls.stdout.strip() == "", (
        f"add -N 占位必须在 diff 后清理出 index，实际残留: {ls.stdout!r}"
    )


def test_git_baseline_text_locale_free_semantics(tmp_path, monkeypatch):
    """sibling：基线语义必须 locale 无关——新建文件→空串（diff 基线），tracked→HEAD 内容，
    非 git 仓→None。修前靠英文 stderr 文案匹配，中文 locale 下新建文件错回 None。"""
    from swarm.worker.executor import WorkerExecutor

    repo = _make_repo(tmp_path)
    stub = SimpleNamespace()
    baseline = WorkerExecutor._git_baseline_text.__get__(stub)
    # 中文 locale 下语义也必须一致（修前此处 new file 会错回 None）
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    assert baseline(repo, "mod.py") == "# HEAD clean\n"
    assert baseline(repo, "not-in-head.txt") == "", "HEAD 无此文件 → 基线=空串（locale 无关）"
    # 深层嵌套路径（复核 LOW）：非 -r 的 ls-tree 对显式深层 pathspec 会沿树下钻，语义一致
    assert baseline(repo, "a/b/deep-new.txt") == "", "深层新建路径同样应判基线空串"
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    assert baseline(non_repo, "x.txt") is None
