"""round29 治本（task d37a52a3 实测）：MERGE apply-check 必须对齐【纯净 base 树】而非脏工作树。

真死因：pull-back 把已完成子任务产物 materialize 进工作区后，工作树里已有 merged_diff 中本应
"新建"的文件（如 ruoyi-alarm/**）。verify_merged_patch_applies 原先跑 `git apply --check`（校验
【工作树】），create 补丁撞上"already exists in working directory" → apply_ok=False → fail-closed
升级人工 → 本应 PARTIAL 的任务被打成 FAILED（77 文件全中）。

但 merged_diff 由 base_reader 相对 git HEAD/钉扎 base 生成（round21 治本），校验基线必须同源：
对 base 树校验，则"工作树已存在但 HEAD 无"的新文件补丁应【通过】。
"""

import os
import subprocess
import tempfile
from pathlib import Path

from swarm.brain.merge_engine import verify_merged_patch_applies


def _temp_git_repo(committed: dict[str, str]) -> str:
    d = tempfile.mkdtemp(prefix="mergeR29_")
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
    for p, c in committed.items():
        fp = Path(d, p)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(c)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=d, check=True)
    return d


def _materialize_untracked(repo: str, rel: str, content: str) -> None:
    """模拟 pull-back：把新文件写进工作区但【不 add/commit】(untracked，HEAD 无)。"""
    fp = Path(repo, rel)
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(content)


_NEWFILE_DIFF = (
    "diff --git a/ruoyi-alarm/src/A.java b/ruoyi-alarm/src/A.java\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/ruoyi-alarm/src/A.java\n"
    "@@ -0,0 +1,2 @@\n"
    "+package com.ruoyi.alarm;\n"
    "+public class A {}\n"
)


def test_create_patch_passes_when_file_only_in_dirty_worktree():
    """核心复现：新文件在脏工作树里已存在（pull-back），但 HEAD 无 → 对 base 校验必须通过。"""
    repo = _temp_git_repo({"README.md": "x\n"})
    # pull-back 已把 A.java materialize 进工作区（untracked）
    _materialize_untracked(repo, "ruoyi-alarm/src/A.java", "package com.ruoyi.alarm;\npublic class A {}\n")

    # 旧行为佐证：直接对工作树 apply --check 必失败(already exists)——证明这是真死因，非造测
    direct = subprocess.run(
        ["git", "apply", "--check", "-"], cwd=repo,
        input=_NEWFILE_DIFF, capture_output=True, text=True,
    )
    # locale 无关：本地 git 可能中文输出("已经存在于工作区中")，只断言【失败】这一前提事实
    assert direct.returncode != 0, "前提：脏工作树上 create 补丁本应 already-exists 失败"
    _msg = (direct.stderr + direct.stdout).lower()
    assert "already exists" in _msg or "已经存在" in _msg, f"应为 already-exists 类失败，实际: {_msg!r}"

    # 治本：对 base 树校验 → 通过（HEAD 无此文件 → 合法 create 补丁）
    ok, err = verify_merged_patch_applies(repo, _NEWFILE_DIFF, base_ref="HEAD")
    assert ok, f"对 base 树校验 create 补丁应通过（工作树污染不该误判），err={err!r}"
    print("  ✅ round29：脏工作树里已存在的新文件，create 补丁对 base 校验通过")


def test_base_check_does_not_touch_worktree_or_index():
    """校验只读：不得改动工作树文件、暂存区，或真 index。"""
    repo = _temp_git_repo({"README.md": "x\n"})
    _materialize_untracked(repo, "ruoyi-alarm/src/A.java", "orig\n")

    before_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
    ).stdout
    before_content = Path(repo, "ruoyi-alarm/src/A.java").read_text()

    verify_merged_patch_applies(repo, _NEWFILE_DIFF, base_ref="HEAD")

    after_status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True,
    ).stdout
    after_content = Path(repo, "ruoyi-alarm/src/A.java").read_text()
    assert before_status == after_status, "apply-check 不得改动暂存区/工作树状态"
    assert before_content == after_content, "apply-check 不得改动工作树文件内容"
    print("  ✅ round29：apply-check 对工作树/index 只读无副作用")


def test_modify_patch_validates_against_base_blob_not_dirty_worktree():
    """modify 补丁：context 应对 base blob 校验，工作树被别的改动弄脏不影响判定。"""
    repo = _temp_git_repo({"Foo.java": "line1\nline2\nline3\n"})
    # 工作树把 Foo.java 改脏（模拟其它子任务/污染），但 merged_diff 相对 base 生成
    Path(repo, "Foo.java").write_text("WHOLLY DIFFERENT\n")
    modify_diff = (
        "diff --git a/Foo.java b/Foo.java\n"
        "--- a/Foo.java\n"
        "+++ b/Foo.java\n"
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        "+INSERTED\n"
        " line2\n"
        " line3\n"
    )
    ok, err = verify_merged_patch_applies(repo, modify_diff, base_ref="HEAD")
    assert ok, f"modify 补丁应对 base blob 校验通过（工作树脏不影响），err={err!r}"
    print("  ✅ round29：modify 补丁对 base blob 校验，工作树脏不误判")


def test_corrupt_patch_still_rejected_against_base():
    """回归：畸形补丁(round16 -0,1 病)对 base 校验仍必须被判失败（fail-closed 不放水）。"""
    repo = _temp_git_repo({"README.md": "x\n"})
    corrupt = (
        "--- a/Bad.java\n"
        "+++ b/Bad.java\n"
        "@@ -0,1 +1,2 @@\n"
        "+package com.x;\n"
        "+public class Bad {}\n"
    )
    ok, err = verify_merged_patch_applies(repo, corrupt, base_ref="HEAD")
    assert not ok, "畸形 -0,1 补丁对 base 校验仍必须失败"
    assert err
    print("  ✅ round29：畸形补丁对 base 校验仍 fail-closed")


def test_bad_base_ref_falls_back_not_crash():
    """base_ref 不可达(GC/坏 ref) → 不崩，退回可用基线；合法新文件仍应能判定。
    复核 finding1：退回 HEAD 是【换校验基线】，成功路径也必须留下 loud 痕迹（降级可观测红线）。"""
    import logging
    # 乱序鲁棒：全套件里有测试用 logging.disable / 祖先 logger propagate=False 污染全局日志态，
    # caplog（依赖传播到 root）会失灵漏抓。改为把捕获 handler 直接挂在【发射 logger】上——
    # 对 propagate/root 配置免疫；logging.disable 仍需复位（它在 emit 源头全局闸断）。
    logging.disable(logging.NOTSET)
    _lg = logging.getLogger("swarm.brain.merge_engine")
    _records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            _records.append(record)

    _h = _Capture(level=logging.WARNING)
    _lvl = _lg.level
    _lg.addHandler(_h)
    _lg.setLevel(logging.NOTSET)
    try:
        repo = _temp_git_repo({"README.md": "x\n"})
        ok, err = verify_merged_patch_applies(
            repo, _NEWFILE_DIFF, base_ref="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        )
    finally:
        _lg.removeHandler(_h)
        _lg.setLevel(_lvl)
    # 坏 ref 退回 HEAD 树校验 → 新文件 HEAD 无 → 通过；关键是不得抛异常/静默假绿
    assert ok, f"坏 base_ref 应退回可用基线并正确判定，err={err!r}"
    # 成功路径也必须 loud（否则 base GC + HEAD 漂移时对不同源树假绿无痕迹）
    assert any("不可达" in r.getMessage() and "退回 HEAD" in r.getMessage() for r in _records), \
        "退回 HEAD 必须 warn（finding1：成功路径不得静默替换基线）"
    print("  ✅ round29：坏 base_ref 优雅退回不崩，且替换基线 loud 可观测")


def test_crlf_base_lf_diff_passes_ignore_whitespace():
    """保真度：base 文件是 CRLF(RuoYi)、merged_diff 是 LF context → 与真 apply 同带
    --ignore-whitespace，check 不得因行尾差异假失败(否则 CRLF 项目又被误升级)。"""
    repo = _temp_git_repo({"README.md": "x\n"})
    # 手写 CRLF 文件并提交进 base（git add 保留字节，禁 autocrlf 转换）
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    Path(repo, "Foo.java").write_bytes(b"line1\r\nline2\r\nline3\r\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "crlf"], cwd=repo, check=True)
    # LF context 的 modify 补丁（worker 归一化产出）
    lf_diff = (
        "diff --git a/Foo.java b/Foo.java\n"
        "--- a/Foo.java\n"
        "+++ b/Foo.java\n"
        "@@ -1,3 +1,4 @@\n"
        " line1\n"
        "+INSERTED\n"
        " line2\n"
        " line3\n"
    )
    ok, err = verify_merged_patch_applies(repo, lf_diff, base_ref="HEAD")
    assert ok, f"CRLF base + LF diff 应 --ignore-whitespace 通过(与真 apply 同源)，err={err!r}"
    print("  ✅ round29：CRLF base + LF diff check 保真通过（对齐真 apply 的 --ignore-whitespace）")


def test_default_base_ref_is_head():
    """默认 base_ref='HEAD'：老调用点不传参也走 base 树校验（零回归）。"""
    repo = _temp_git_repo({"README.md": "x\n"})
    _materialize_untracked(repo, "ruoyi-alarm/src/A.java", "package com.ruoyi.alarm;\npublic class A {}\n")
    ok, _ = verify_merged_patch_applies(repo, _NEWFILE_DIFF)  # 不传 base_ref
    assert ok, "默认 HEAD 也应对 base 校验（脏工作树不误判）"
    print("  ✅ round29：默认 base_ref=HEAD，老调用点零回归受益")
