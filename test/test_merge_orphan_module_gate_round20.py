"""#11(c) 复现+治本：MERGE 硬门控【模块骨架缺失】子任务。

根因（round19）：module-defining 子任务（建 <dir>/pom.xml）不在成功集时，MERGE 仍纳入
【引用该模块的兄弟补丁】(<dir>/src/**) + 根 pom 注册 <module>dir</module> → 合并 patch
里有模块目录文件却无该模块 pom 骨架 → git apply / reactor 崩（No such file / Child module
does not exist），交付死于门口。

治本：合并前排除【引用了骨架缺失模块的补丁】——<dir>/pom.xml 既不在本次合并集、也不在 repo
base → 该 <dir> 下所有补丁剔除（保其余模块正常交付），并显式记原因，不放任裸 apply 崩。
跨栈通用：只按"模块目录顶层是否有该栈的清单"判，不写死 pom（Maven pom.xml / Gradle
build.gradle / Cargo Cargo.toml 均可插）。
"""

from __future__ import annotations

from swarm.brain.merge_engine import filter_orphan_module_patches


def _newfile_diff(path: str, body: str = "x") -> str:
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,1 @@\n+{body}\n"


def test_orphan_module_patches_excluded():
    """ruoyi-alarm 骨架(pom)在成功集，ruoyi-alarm-sdk 骨架缺失 → 只剔 sdk 补丁。"""
    diffs = [
        ("st-1-1", _newfile_diff("ruoyi-alarm/pom.xml", "<project/>")),
        ("st-9", _newfile_diff("ruoyi-alarm/src/main/java/A.java")),
        # sdk 骨架子任务 abandon → 不在成功集；但有兄弟写了 sdk 下的文件
        ("st-5", _newfile_diff("ruoyi-alarm-sdk/src/main/java/B.java")),
    ]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False)
    kept = {sid for sid, _ in filtered}
    assert kept == {"st-1-1", "st-9"}
    assert "ruoyi-alarm-sdk" in dropped
    assert dropped["ruoyi-alarm-sdk"] == ["st-5"]


def test_module_defined_in_base_not_dropped():
    """模块 pom 已在 repo base（历史模块）→ 不剔（骨架在 base 就绪）。"""
    diffs = [("st-7", _newfile_diff("ruoyi-common/src/main/java/C.java"))]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: d == "ruoyi-common")
    assert {sid for sid, _ in filtered} == {"st-7"}
    assert dropped == {}


def test_module_defined_in_merge_kept():
    """模块 pom 在本次合并集 → 骨架落盘，兄弟补丁全保留。"""
    diffs = [
        ("st-1", _newfile_diff("ruoyi-alarm/pom.xml", "<project/>")),
        ("st-2", _newfile_diff("ruoyi-alarm/src/main/java/A.java")),
    ]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False)
    assert {sid for sid, _ in filtered} == {"st-1", "st-2"}
    assert dropped == {}


def test_gradle_manifest_also_defines_module():
    """跨栈：build.gradle 同样算模块骨架（不写死 pom）。"""
    diffs = [
        ("st-1", _newfile_diff("feature-a/build.gradle", "plugins {}")),
        ("st-2", _newfile_diff("feature-a/src/main/kotlin/A.kt")),
        ("st-3", _newfile_diff("feature-b/src/main/kotlin/B.kt")),  # 无 build.gradle → 孤儿
    ]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False)
    assert {sid for sid, _ in filtered} == {"st-1", "st-2"}
    assert dropped.get("feature-b") == ["st-3"]


def test_root_level_files_never_dropped():
    """根级文件(无模块前缀，如根 pom / README)永不被当孤儿模块剔除。"""
    diffs = [
        ("st-0", _newfile_diff("pom.xml", "<project/>")),
        ("st-1", _newfile_diff("README.md")),
    ]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False)
    assert {sid for sid, _ in filtered} == {"st-0", "st-1"}
    assert dropped == {}


def test_no_orphans_returns_input_unchanged():
    diffs = [("st-1", _newfile_diff("ruoyi-alarm/pom.xml"))]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False)
    assert filtered == diffs
    assert dropped == {}


# ════════════════ #36 治本：单根项目子目录非构建单元，绝不 orphan-drop ════════════════

def test_36_single_root_go_subdir_not_orphaned():
    """CRITICAL 治本：Go 单模块(单根 go.mod)——internal/svc/foo.go 的顶层目录 internal 无
    internal/go.mod，旧行为误判孤儿→整份补丁静默丢弃。is_multimodule=False → 整体让路，保留。"""
    diffs = [("st-1", _newfile_diff("internal/svc/foo.go", "package svc"))]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False, is_multimodule=False)
    assert {sid for sid, _ in filtered} == {"st-1"}, filtered
    assert dropped == {}, dropped


def test_36_single_root_python_package_kept():
    """Python 包目录(无 per-dir manifest)不是构建单元 → 补丁保留。"""
    diffs = [("st-1", _newfile_diff("app/models/user.py", "class User: pass"))]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False, is_multimodule=False)
    assert {sid for sid, _ in filtered} == {"st-1"} and dropped == {}


def test_36_multimodule_still_drops_orphan():
    """回归：多模块布局(is_multimodule=True)下真孤儿仍剔——Maven 保护不减。"""
    diffs = [("st-5", _newfile_diff("ruoyi-alarm-sdk/src/main/java/B.java"))]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False, is_multimodule=True)
    assert {sid for sid, _ in filtered} == set()
    assert dropped.get("ruoyi-alarm-sdk") == ["st-5"]


def _plan_with(*manifest_paths):
    from types import SimpleNamespace
    return SimpleNamespace(subtasks=[
        SimpleNamespace(scope=SimpleNamespace(create_files=list(manifest_paths), writable=[]))])


def test_36_plan_signal_greenfield_multimodule():
    """复核残留治本：greenfield 首轮磁盘无根 pom(project_path=None)，但 plan 声明 moduleA/pom.xml
    → 计划信号判多模块 → orphan 过滤仍会跑(不因 scaffold 全失败静默漏 orphan)。"""
    from swarm.brain.nodes import _detect_multimodule_layout
    assert _detect_multimodule_layout(None, _plan_with("moduleA/pom.xml")) is True


def test_36_plan_signal_single_root_false():
    """单根：plan 只声明根级/源码文件(非 <dir>/模块清单)→ 计划信号不触发；无磁盘 → False。"""
    from swarm.brain.nodes import _detect_multimodule_layout
    assert _detect_multimodule_layout(None, _plan_with("go.mod", "internal/svc/foo.go")) is False


def test_36_disk_signal_root_pom_modules(tmp_path):
    """磁盘信号：根 pom 含 <modules> → 多模块。"""
    from swarm.brain.nodes import _detect_multimodule_layout
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>a</module></modules></project>", encoding="utf-8")
    assert _detect_multimodule_layout(str(tmp_path), None) is True


def test_36_disk_signal_single_module_maven_false(tmp_path):
    """单模块 Maven(根 pom 无 <modules>)→ 单根 → False(子目录 src 不该 orphan-drop)。"""
    from swarm.brain.nodes import _detect_multimodule_layout
    (tmp_path / "pom.xml").write_text("<project><artifactId>x</artifactId></project>", encoding="utf-8")
    assert _detect_multimodule_layout(str(tmp_path), None) is False


def test_36_disk_signal_subdir_manifest(tmp_path):
    """磁盘信号：任一顶层子目录已含自己的模块清单(go.mod)→ 每目录模块布局。"""
    from swarm.brain.nodes import _detect_multimodule_layout
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "go.mod").write_text("module x", encoding="utf-8")
    assert _detect_multimodule_layout(str(tmp_path), None) is True


def test_36_defined_overrides_single_root_flag():
    """即便传 is_multimodule=False，本批确有模块清单落盘(defined 非空)→ 证明每目录模块布局
    → 过滤照常(真孤儿仍剔)。"""
    diffs = [
        ("st-1", _newfile_diff("mod-a/pom.xml", "<project/>")),
        ("st-2", _newfile_diff("mod-b/src/main/java/B.java")),  # 无 mod-b 骨架 → 孤儿
    ]
    filtered, dropped = filter_orphan_module_patches(
        diffs, base_module_exists=lambda d: False, is_multimodule=False)
    assert {sid for sid, _ in filtered} == {"st-1"}
    assert dropped.get("mod-b") == ["st-2"]
