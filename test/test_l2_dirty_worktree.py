"""task fdaa1932 回归：VERIFY_L2 integration_review 必须在干净 HEAD 上做 git apply --check。

根因：worker pull-back 把改动写进本地 project_path 工作区 → 工作区脏（补丁改动已存在）。
integration_review 直接在脏工作区 git apply --check → context 不匹配 → "补丁未应用"假阴性
→ medium 任务 VERIFY_L2 永远失败、replan 死循环。
修复：① _reset_worktree_to_head 先把补丁涉及文件 reset 到 HEAD；
     ② _detect_build_cmd 检测构建工具本机可用性，缺工具时跳过本机编译（不误判）。
"""
import os
import shutil
import subprocess
import tempfile

from swarm.brain.integration_review import (
    _detect_build_cmd_generic,
    _reset_worktree_to_head,
    run_integration_review,
)


def _init_repo() -> str:
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    with open(os.path.join(d, "A.java"), "w") as f:
        f.write("class A {\n    void f() {}\n}\n")
    subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        capture_output=True, check=False,
    )
    return d


def test_integration_review_resets_dirty_worktree():
    """脏工作区（改动已写入）→ integration_review 先 reset 再 check → apply_check 通过。"""
    d = _init_repo()
    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,4 @@\n class A {\n+    void g() {}\n     void f() {}\n }\n"
    # 先把工作区弄脏（直接把改动写进文件，模拟 worker pull-back）
    with open(os.path.join(d, "A.java"), "w") as f:
        f.write("class A {\n    void g() {}\n    void f() {}\n}\n")
    # 脏工作区直接 git apply --check 会失败（改动已存在）
    from swarm.project.diff_apply import apply_git_diff
    assert not apply_git_diff(d, diff, check_only=True).get("ok"), "脏工作区应复现失败"
    # _reset_worktree_to_head 后应能 check 通过
    _reset_worktree_to_head(d, diff)
    assert apply_git_diff(d, diff, check_only=True).get("ok"), "reset 到 HEAD 后应通过"


def test_integration_review_full_dirty_worktree():
    """完整 run_integration_review：脏工作区 → 内部 reset → 整体通过（无构建工具时跳过编译）。"""
    d = _init_repo()
    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,4 @@\n class A {\n+    void g() {}\n     void f() {}\n }\n"
    # 弄脏
    with open(os.path.join(d, "A.java"), "w") as f:
        f.write("class A {\n    void g() {}\n    void f() {}\n}\n")
    ok, issues, details = run_integration_review(d, diff)
    assert details.get("apply_check") is True, f"apply_check 应通过: {issues}"
    # 无 pom.xml/package.json → 无构建命令 → 不因编译误判
    assert ok, f"整体应通过: {issues}"


def test_reset_worktree_preserves_unrelated_changes():
    """R1 回归：scoped 回滚只动 diff 涉及文件，绝不抹用户【无关】的未提交改动/未跟踪文件
    （原 `git checkout -- .` + `git clean -fd` 会全清——本测试守住治本）。"""
    d = _init_repo()  # 已跟踪 A.java
    # 用户无关改动：① 改一个已跟踪文件(非 diff 目标) ② 一个未跟踪新文件
    with open(os.path.join(d, "B_tracked.txt"), "w") as f:
        f.write("v1\n")
    subprocess.run(["git", "-C", d, "add", "B_tracked.txt"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"],
        capture_output=True, check=False,
    )
    with open(os.path.join(d, "B_tracked.txt"), "w") as f:
        f.write("v2-uncommitted-user-edit\n")          # 已跟踪文件的未提交改动
    with open(os.path.join(d, "user_scratch.txt"), "w") as f:
        f.write("important untracked note\n")           # 未跟踪文件

    # diff 只涉及 A.java
    diff = "--- a/A.java\n+++ b/A.java\n@@ -1,3 +1,4 @@\n class A {\n+    void g() {}\n     void f() {}\n }\n"
    with open(os.path.join(d, "A.java"), "w") as f:
        f.write("class A {\n    void g() {}\n    void f() {}\n}\n")

    _reset_worktree_to_head(d, diff)

    # A.java 被回滚到 HEAD；用户无关改动【完整保留】
    assert open(os.path.join(d, "B_tracked.txt")).read() == "v2-uncommitted-user-edit\n", \
        "无关已跟踪文件的未提交改动不应被抹除"
    assert os.path.isfile(os.path.join(d, "user_scratch.txt")), \
        "无关未跟踪文件不应被 clean 掉"


def test_detect_build_cmd_generic_returns_cmd_regardless_of_local_tool():
    """治本 round21：`_detect_build_cmd_generic` 据构建文件出命令，【不】gate 本机工具可用性——
    编译在项目沙箱按检测版本工具链跑，本机有没有 mvn 不影响"该不该编译"的判定。返回 None 仅当
    真无构建文件(纯 docs)。杜绝旧行为"本机无 mvn→None→静默跳过编译→L2 假绿"。"""
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "pom.xml"), "w") as f:
        f.write("<project></project>")
    cmd = _detect_build_cmd_generic(d)
    assert cmd and "mvn" in cmd, "有 pom.xml 即应返回 mvn 编译命令(与本机是否装 mvn 无关)"
    # 真无构建文件 → None（合理跳过，非降级）
    d2 = tempfile.mkdtemp()
    assert _detect_build_cmd_generic(d2) is None


def test_reset_worktree_removes_new_file_residue():
    """task 691c1670：新建文件被 worker pull-back 残留工作区时，reset 应删除残留，
    让 git apply 能干净新建（不再"补丁未应用"）。"""
    d = _init_repo()  # 只有 A.java
    # 新建文件的 diff（New.java HEAD 不存在）
    diff = "--- /dev/null\n+++ b/New.java\n@@ -0,0 +1,2 @@\n+class New {\n+}\n"
    # 模拟 worker pull-back：New.java 已残留工作区
    with open(os.path.join(d, "New.java"), "w") as f:
        f.write("class New {\n}\n")
    from swarm.project.diff_apply import apply_git_diff
    # 残留时 apply --check 失败（文件已存在）
    assert not apply_git_diff(d, diff, check_only=True).get("ok"), "残留新文件应致 apply 失败"
    # reset 删掉残留后通过
    _reset_worktree_to_head(d, diff)
    assert not os.path.isfile(os.path.join(d, "New.java")), "新文件残留应被删除"
    assert apply_git_diff(d, diff, check_only=True).get("ok"), "删除残留后应能干净新建"
