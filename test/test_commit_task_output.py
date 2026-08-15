"""第二批根因(选项A)：commit_task_output —— 任务产出本地 git commit。

产出 apply 后若不 commit，会被后续 git checkout / reset / 下个任务冲掉 → 事实库滞后。
commit 后稳定落盘。仅本地，不 push。
"""
import subprocess
import tempfile
from unittest.mock import patch

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
    """★批25 GS-5w 换锁★（原命题：源码不含 "push"/"git push" 调用字面——getsource
    字面量断言；现改为行为锁）：有改动场景真跑 commit_task_output，spy subprocess.run
    抓全部 git argv——任何一次调用带 push 即红（仅本地 commit，推送由用户拍板）。
    区分力：在 commit_task_output 里补一句 git push 调用 → 红。"""
    import swarm.project.diff_apply as da

    real_run = subprocess.run
    git_argv: list[list[str]] = []

    def _spy(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and "git" in str(cmd[0]):
            git_argv.append(list(cmd))
        return real_run(cmd, *a, **k)

    d = _init_repo()
    with open(f"{d}/New.java", "w") as f:
        f.write("class New {}\n")
    with patch.object(da.subprocess, "run", _spy):
        r = commit_task_output(d, ["New.java"], task_id="t-nopush")
    # 前提自证（T-A7）：必须真走到 commit——否则 spy 没覆盖「顺手 push」最可能藏的路径
    assert r["ok"] and r["committed"], f"夹具前提失效：有改动场景必须真提交: {r}"
    assert git_argv, "夹具前提失效：commit_task_output 未发起任何 git 调用"
    assert all("push" not in argv for argv in git_argv), \
        f"commit_task_output 绝不许 push（仅本地 commit）: {git_argv}"
