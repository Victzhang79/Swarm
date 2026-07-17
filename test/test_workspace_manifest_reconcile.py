#!/usr/bin/env python3
"""通用 workspace 聚合清单对账(确定性、幂等、模型无关)回归。

治本主题：并行子任务在独立沙箱改共享聚合清单 → pull-back 整文件覆盖把成员注册冲掉 →
构建找不到模块确定性失败。对账器据【磁盘 ground truth】补齐成员，三处复用。
覆盖 Maven/Gradle/Cargo/.NET/Go 五生态 + 幂等 + 不碰独立工程/glob 覆盖/动态枚举。
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from swarm.worker.workspace_manifest import reconcile_workspace_manifests


def _mk() -> Path:
    return Path(tempfile.mkdtemp(prefix="swarm_wm_"))


# ───────── Maven ─────────
def test_maven_registers_missing_child():
    root = _mk()
    (root / "pom.xml").write_text(
        "<project><modules>\n<module>admin</module>\n</modules></project>", "utf-8")
    for m in ("admin", "alarm"):
        (root / m).mkdir()
    (root / "admin" / "pom.xml").write_text("<project><parent/></project>", "utf-8")
    (root / "alarm" / "pom.xml").write_text("<project><parent/></project>", "utf-8")
    # 独立工程(无 parent)不碰
    (root / "vendor").mkdir()
    (root / "vendor" / "pom.xml").write_text("<project></project>", "utf-8")

    r = reconcile_workspace_manifests(str(root))
    assert "pom.xml" in r["modified_manifests"]
    assert r["added"]["pom.xml"] == ["alarm"]
    txt = (root / "pom.xml").read_text()
    assert "<module>alarm</module>" in txt
    assert "<module>vendor</module>" not in txt
    # 幂等
    r2 = reconcile_workspace_manifests(str(root))
    assert r2["modified_manifests"] == []
    assert (root / "pom.xml").read_text().count("<module>alarm</module>") == 1


def test_maven_nested_aggregator():
    root = _mk()
    (root / "pom.xml").write_text("<project><modules><module>grp</module></modules></project>", "utf-8")
    (root / "grp").mkdir()
    # 嵌套聚合器
    (root / "grp" / "pom.xml").write_text(
        "<project><parent/><modules></modules></project>", "utf-8")
    (root / "grp" / "leaf").mkdir()
    (root / "grp" / "leaf" / "pom.xml").write_text("<project><parent/></project>", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert "grp/pom.xml" in r["modified_manifests"]
    assert "<module>leaf</module>" in (root / "grp" / "pom.xml").read_text()


# ───────── Gradle ─────────
def test_gradle_registers_missing_include():
    root = _mk()
    (root / "settings.gradle").write_text("include ':app'\n", "utf-8")
    for m in ("app", "core"):
        (root / m).mkdir()
        (root / m / "build.gradle").write_text("", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert "settings.gradle" in r["modified_manifests"]
    assert r["added"]["settings.gradle"] == ["core"]
    assert "include ':core'" in (root / "settings.gradle").read_text()
    # 幂等
    assert reconcile_workspace_manifests(str(root))["modified_manifests"] == []


def test_gradle_dynamic_enumeration_skipped():
    root = _mk()
    (root / "settings.gradle").write_text(
        "rootDir.eachDir { include it.name }\n", "utf-8")
    (root / "core").mkdir()
    (root / "core" / "build.gradle").write_text("", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert r["modified_manifests"] == []  # 动态枚举不碰


def test_gradle_kts():
    root = _mk()
    (root / "settings.gradle.kts").write_text('include(":app")\n', "utf-8")
    for m in ("app", "data"):
        (root / m).mkdir()
        (root / m / "build.gradle.kts").write_text("", "utf-8")
    reconcile_workspace_manifests(str(root))
    assert 'include(":data")' in (root / "settings.gradle.kts").read_text()


# ───────── Cargo ─────────
def test_cargo_registers_missing_member():
    root = _mk()
    (root / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["app"]\n', "utf-8")
    for m in ("app", "lib"):
        (root / m).mkdir()
        (root / m / "Cargo.toml").write_text(f'[package]\nname = "{m}"\n', "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert "Cargo.toml" in r["modified_manifests"]
    assert "lib" in r["added"]["Cargo.toml"]
    txt = (root / "Cargo.toml").read_text()
    assert re.search(r'members\s*=\s*\[.*"lib".*\]', txt, re.S)
    # 幂等
    assert reconcile_workspace_manifests(str(root))["modified_manifests"] == []


def test_cargo_comment_in_members_skipped():
    root = _mk()
    (root / "Cargo.toml").write_text(
        '[workspace]\nmembers = [\n  "app", # keep first\n]\n', "utf-8")
    for m in ("app", "lib"):
        (root / m).mkdir()
        (root / m / "Cargo.toml").write_text(f'[package]\nname="{m}"\n', "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert r["modified_manifests"] == []  # 含注释 → 不碰(保注释/保幂等)
    assert "# keep first" in (root / "Cargo.toml").read_text()


def test_cargo_glob_covered_skipped():
    root = _mk()
    (root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n', "utf-8")
    (root / "crates").mkdir()
    (root / "crates" / "a").mkdir()
    (root / "crates" / "a" / "Cargo.toml").write_text('[package]\nname="a"\n', "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert r["modified_manifests"] == []  # glob 已覆盖，不补


# ───────── .NET (.sln) ─────────
def test_dotnet_sln_registers_project():
    root = _mk()
    (root / "app.sln").write_text(
        "Microsoft Visual Studio Solution File\n"
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "App", "App\\App.csproj", '
        '"{11111111-1111-1111-1111-111111111111}"\nEndProject\n'
        "Global\n"
        "\tGlobalSection(ProjectConfigurationPlatforms) = postSolution\n"
        "\tEndGlobalSection\n"
        "EndGlobal\n", "utf-8")
    (root / "App").mkdir()
    (root / "App" / "App.csproj").write_text("<Project/>", "utf-8")
    (root / "Lib").mkdir()
    (root / "Lib" / "Lib.csproj").write_text("<Project/>", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert "app.sln" in r["modified_manifests"]
    assert "Lib" in r["added"]["app.sln"]
    txt = (root / "app.sln").read_text()
    assert '"Lib"' in txt and "Lib\\Lib.csproj" in txt
    assert "ActiveCfg = Debug|Any CPU" in txt
    # 幂等(已引用的不重复)
    assert reconcile_workspace_manifests(str(root))["modified_manifests"] == []


def test_dotnet_sln_missing_config_section_skipped():
    root = _mk()
    # 无 GlobalSection(ProjectConfigurationPlatforms) → 跳过，避免产出损坏 sln
    (root / "app.sln").write_text(
        "Microsoft Visual Studio Solution File\n"
        "Global\nEndGlobal\n", "utf-8")
    (root / "Lib").mkdir()
    (root / "Lib" / "Lib.csproj").write_text("<Project/>", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert r["modified_manifests"] == []
    assert "Lib" not in (root / "app.sln").read_text()


# ───────── Go (go.work) ─────────
def test_go_work_registers_use():
    root = _mk()
    (root / "go.work").write_text("go 1.21\n\nuse ./a\n", "utf-8")
    for m in ("a", "b"):
        (root / m).mkdir()
        (root / m / "go.mod").write_text(f"module example.com/{m}\n", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert "go.work" in r["modified_manifests"]
    assert "b" in r["added"]["go.work"]
    assert "use ./b" in (root / "go.work").read_text()
    assert reconcile_workspace_manifests(str(root))["modified_manifests"] == []


def test_go_work_absent_not_created():
    root = _mk()
    (root / "a").mkdir()
    (root / "a" / "go.mod").write_text("module example.com/a\n", "utf-8")
    r = reconcile_workspace_manifests(str(root))
    assert r["modified_manifests"] == []
    assert not (root / "go.work").exists()  # 绝不擅自创建


# ───────── 通用：无聚合清单不动 ─────────
def test_no_aggregate_manifest_noop():
    root = _mk()
    (root / "README.md").write_text("hi", "utf-8")
    assert reconcile_workspace_manifests(str(root)) == {
        "modified_manifests": [], "added": {}, "removed": {}}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")


# ── R65REPLAY-T3（task #68）：depMgmt 只登【被依赖的】子模块，应用壳不入账 ──

def _mk_maven_tree(tmp_path):
    """root(带 depMgmt 块) + lib(被 app 依赖) + app(应用壳，无人依赖)。"""
    root = tmp_path / "proj"
    (root / "lib").mkdir(parents=True)
    (root / "app").mkdir(parents=True)
    (root / "pom.xml").write_text(
        "<project><groupId>com.x</groupId><artifactId>root</artifactId>"
        "<version>1.0</version><packaging>pom</packaging>"
        "<modules><module>lib</module><module>app</module></modules>"
        "<dependencyManagement>\n  <dependencies>\n  </dependencies>\n"
        "</dependencyManagement></project>")
    (root / "lib" / "pom.xml").write_text(
        "<project><parent><groupId>com.x</groupId><artifactId>root</artifactId>"
        "<version>1.0</version></parent><artifactId>lib</artifactId></project>")
    (root / "app" / "pom.xml").write_text(
        "<project><parent><groupId>com.x</groupId><artifactId>root</artifactId>"
        "<version>1.0</version></parent><artifactId>app</artifactId>"
        "<dependencies><dependency><groupId>com.x</groupId>"
        "<artifactId>lib</artifactId></dependency></dependencies></project>")
    return root


def test_depmgmt_only_registers_referenced_modules(tmp_path):
    """★回放毒株本体★：D2 补写把【无人依赖的应用壳】(ruoyi-admin 形态)也登进根
    depMgmt=错误声明可依赖件（回放实锤 com.ruoyi:ruoyi-admin:4.8.3 存活至终态树）。
    治=只补【被本工程其它模块运行时依赖引用】的 (g,a)——与 round18 治本初衷
    （内部依赖版本可解析）精确对齐；应用壳没人依赖自然不入账。"""
    from swarm.worker.workspace_manifest import _reconcile_maven_dep_versions
    root = _mk_maven_tree(tmp_path)
    modified, added = _reconcile_maven_dep_versions(root, [])
    dm = (root / "pom.xml").read_text()
    assert "<artifactId>lib</artifactId>" in dm.split("dependencyManagement")[1], \
        "被依赖的 lib 必须补进 depMgmt（round18 语义不回归）"
    assert "com.x:lib:1.0" in (added.get("pom.xml") or []), added
    assert "<artifactId>app</artifactId>" not in dm.split("dependencyManagement")[1], \
        "无人依赖的应用壳被登成可依赖件=回放根 pom 毒株死型"


def test_depmgmt_idempotent_after_referenced_fix(tmp_path):
    """幂等回归：已管理的 (g,a) 二次跑零变更。"""
    from swarm.worker.workspace_manifest import _reconcile_maven_dep_versions
    root = _mk_maven_tree(tmp_path)
    _reconcile_maven_dep_versions(root, [])
    modified2, added2 = _reconcile_maven_dep_versions(root, [])
    assert not modified2 and not added2
