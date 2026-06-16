"""第二批根因(选项A)：commit_task_output —— 任务产出本地 git commit。

产出 apply 后若不 commit，会被后续 git checkout / reset / 下个任务冲掉 → 事实库滞后。
commit 后稳定落盘。仅本地，不 push。
"""
import subprocess
import tempfile

from swarm.project.diff_apply import commit_task_output


def _init_repo():
    d = tempfile.mkdtemp()
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    with open(f"{d}/seed.txt", "w") as f:
        f.write("seed\n")
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "seed"], check=True)
    return d


def test_commit_new_file_persists():
    """新建文件 → commit 后 git 跟踪、HEAD 含该文件（不会被 checkout 冲掉）。"""
    d = _init_repo()
    with open(f"{d}/New.java", "w") as f:
        f.write("class New {}\n")
    r = commit_task_output(d, ["New.java"], task_id="t-1")
    assert r["ok"] and r["committed"], r
    # 验证：git checkout . 不再冲掉（已 commit 进 HEAD）
    subprocess.run(["git", "-C", d, "checkout", "--", "."], check=True)
    import os
    assert os.path.isfile(f"{d}/New.java"), "commit 后 checkout 不应冲掉"
    # HEAD 含该文件
    ls = subprocess.run(["git", "-C", d, "ls-files"], capture_output=True, text=True).stdout
    assert "New.java" in ls


def test_commit_no_changes_skips():
    """apply 后内容与 HEAD 相同 → 无暂存改动 → 跳过 commit（不报错）。"""
    d = _init_repo()
    r = commit_task_output(d, ["seed.txt"], task_id="t-2")
    assert r["ok"] and not r["committed"]
    assert "无已暂存改动" in r["reason"]


def test_commit_non_git_noop():
    """非 git 目录 → 跳过不报错。"""
    d = tempfile.mkdtemp()
    with open(f"{d}/x.txt", "w") as f:
        f.write("x")
    r = commit_task_output(d, ["x.txt"])
    assert r["ok"] and not r["committed"]
    assert "非 git" in r["reason"]


def test_commit_empty_files_noop():
    r = commit_task_output("/tmp", [])
    assert r["ok"] and not r["committed"]


def test_commit_does_not_push():
    """确认 commit_task_output 不含 git push 命令（仅本地）。"""
    import inspect

    from swarm.project import diff_apply
    src = inspect.getsource(diff_apply.commit_task_output)
    # 检查无实际 push 命令调用（注释/docstring 里的"push"字样不算）
    assert '"push"' not in src and "'push'" not in src, "不应有 git push 命令"
    assert "git\", \"push" not in src.replace(" ", "")
