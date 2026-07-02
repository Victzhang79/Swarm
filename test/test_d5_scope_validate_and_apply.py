"""D5 治本复现（先于实现）：

(a) validate 去 -am：我 commit f4c1a40 让纯 pom 子任务走 `mvn -q validate`，经 _scope_maven_command
    收窄成 `mvn -pl <mod> -am validate` → -am 拉全上游 reactor → 纯 pom 子任务因【无关 sibling 缺陷】
    被判 hard-FAIL（重演 P1 drag-down，违背 P0-B"不连坐 sibling"）。validate 是模块级弱校验，只该
    校本模块 pom + parent 链，不需上游模块产物 → 去 -am。compile/test 等真需上游产物的 → 保留 -am。

(b) 非原子/分文件 apply（P0-C 鲁棒性）：一个坏 hunk 令整块 `git apply` 原子失败 → 连坐回滚全部好
    文件（round18 ~30 个正确 producer 一个没落盘）。resilient apply：整块失败则按文件段独立 apply，
    好段照常落盘、坏段单独剔除，杜绝连坐。
"""

from __future__ import annotations

import subprocess

from swarm.worker.l1_pipeline import _scope_maven_command

_ROOT = """<project>
  <modules>
    <module>ruoyi-admin</module>
    <module>ruoyi-common</module>
  </modules>
</project>
"""


def _mkproj(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


def _multi_module(tmp_path):
    return _mkproj(tmp_path, {
        "pom.xml": _ROOT,
        "ruoyi-admin/pom.xml": "<project/>",
        "ruoyi-common/pom.xml": "<project/>",
    })


# ── D5(a) validate 去 -am ──────────────────────────────────────────────
def test_validate_drops_am(tmp_path):
    """纯 pom 子任务的 validate 收窄到本模块，但【不加 -am】(不拉上游 sibling，杜绝连坐)。"""
    proj = _multi_module(tmp_path)
    out = _scope_maven_command("mvn -q validate", proj, ["ruoyi-admin/pom.xml"])
    assert "-pl ruoyi-admin" in out, f"应收窄到本模块，实得 {out!r}"
    assert "-am" not in out, f"validate 不得加 -am(否则无关 sibling 缺陷连坐 hard-FAIL)，实得 {out!r}"


def test_compile_keeps_am(tmp_path):
    """compile 真需上游模块产物(classpath) → 保留 -am(不回归)。"""
    proj = _multi_module(tmp_path)
    out = _scope_maven_command("mvn -q compile", proj, ["ruoyi-admin/src/main/java/A.java"])
    assert "-pl ruoyi-admin" in out and "-am" in out, f"compile 应 -pl+-am，实得 {out!r}"


def test_test_phase_keeps_am(tmp_path):
    proj = _multi_module(tmp_path)
    out = _scope_maven_command("mvn -q test", proj, ["ruoyi-admin/src/test/java/AT.java"])
    assert "-am" in out, f"test 需上游产物，应保留 -am，实得 {out!r}"


def test_validate_single_module_no_reactor_unchanged(tmp_path):
    """无多模块(无 <modules>)→ 不改写(无 reactor 无需收窄)。"""
    proj = _mkproj(tmp_path, {"pom.xml": "<project/>"})
    out = _scope_maven_command("mvn -q validate", proj, ["pom.xml"])
    assert out == "mvn -q validate", f"单模块无需收窄，实得 {out!r}"


# ── D5(b) 非原子/分文件 apply ──────────────────────────────────────────
def _git_init(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    return str(tmp_path)


_GOOD = """diff --git a/good/New.java b/good/New.java
new file mode 100644
index 0000000..111
--- /dev/null
+++ b/good/New.java
@@ -0,0 +1,2 @@
+public class New {}
+// ok
"""

# 修改一个【不存在】的文件 → git apply 必失败(坏段)
_BAD = """diff --git a/missing/Old.java b/missing/Old.java
index 111..222 100644
--- a/missing/Old.java
+++ b/missing/Old.java
@@ -1,2 +1,2 @@
-old line
+new line
 context
"""


def test_split_diff_by_file_two_sections():
    from swarm.project.diff_apply import split_diff_by_file
    secs = split_diff_by_file(_GOOD + _BAD)
    assert len(secs) == 2, f"应拆成 2 个文件段，实得 {len(secs)}"
    files = sorted(f for fs, _ in secs for f in fs)
    assert "good/New.java" in files and "missing/Old.java" in files


def test_resilient_apply_lands_good_drops_bad(tmp_path):
    from swarm.project.diff_apply import apply_git_diff_resilient
    proj = _git_init(tmp_path)
    res = apply_git_diff_resilient(proj, _GOOD + _BAD)
    assert res["ok"] is True, "有好文件落盘即 ok"
    assert "good/New.java" in res["applied"], f"好文件应落盘，实得 {res}"
    assert (tmp_path / "good/New.java").is_file(), "好文件必须真写到工作区"
    assert res["failed"], "坏段应被记录剔除"
    bad_files = [f for item in res["failed"] for f in item.get("files", [])]
    assert "missing/Old.java" in bad_files, f"坏文件应在 failed，实得 {res['failed']}"
    assert not (tmp_path / "missing/Old.java").is_file(), "坏文件不应落盘"


def test_resilient_apply_all_good_fast_path(tmp_path):
    from swarm.project.diff_apply import apply_git_diff_resilient
    proj = _git_init(tmp_path)
    res = apply_git_diff_resilient(proj, _GOOD)
    assert res["ok"] and res["stage"] == "apply", f"全好走整块快路径，实得 {res}"
    assert (tmp_path / "good/New.java").is_file()
    assert not res["failed"]


def test_resilient_apply_empty(tmp_path):
    from swarm.project.diff_apply import apply_git_diff_resilient
    res = apply_git_diff_resilient(_git_init(tmp_path), "")
    assert res["ok"] is False and res["applied"] == []


def test_resilient_all_bad_reports_not_ok(tmp_path):
    from swarm.project.diff_apply import apply_git_diff_resilient
    proj = _git_init(tmp_path)
    res = apply_git_diff_resilient(proj, _BAD)
    assert res["ok"] is False, "全坏 → ok=False"
    assert res["applied"] == []


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        import inspect
        if "tmp_path" in inspect.signature(fn).parameters:
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
        else:
            fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== D5 validate-去am + 分文件apply: {len(fns)}/{len(fns)} passed ===")
