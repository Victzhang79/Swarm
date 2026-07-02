"""D2 治本复现：交付前 pom 版本完整性 + reconcile 补 root <dependencyManagement> 版本。

round18 P1/§3 铁证：st-1 在 ruoyi-admin/pom.xml 加 `com.ruoyi:ruoyi-alarm` 依赖【未写 version】，
且 root pom `<dependencyManagement>` 未声明 ruoyi-alarm 版本 → reactor 解析失败 → compile 失败。
这正是"P0-A rebase 保留 st-1 缺版本 pom → 最终 compile 失败"的机制（readiness §3）。
`reconcile_workspace_manifests` 原只补 `<modules>` 注册，**不补 dependencyManagement 版本** → 缺版本无机制补回。

治本（确定性、幂等、模型无关，与 _reconcile_maven 同哲学=据磁盘 ground-truth 对账）：
1. reconcile 扩：对每个【本工程子模块】(声明 <parent> 的模块 pom)，确保 root
   `<dependencyManagement>` 声明其 groupId:artifactId:version（= 模块自身/继承的项目版本）。
   这样任何【版本缺省】的跨模块内部依赖都可经 dependencyManagement 解析。
2. 完整性闸门：扫所有模块 pom 的【内部模块依赖】，凡缺 version 且未被 root dependencyManagement
   覆盖者 → 返回清单（非空 → 交付/构建判 build_ok=False，fail-closed）。

本文件【先于实现】编写。
"""
from __future__ import annotations

from swarm.worker.workspace_manifest import (
    missing_intra_project_module_versions,
    reconcile_workspace_manifests,
)

_ROOT = """<project>
  <groupId>com.ruoyi</groupId>
  <artifactId>ruoyi</artifactId>
  <version>3.8.6</version>
  <packaging>pom</packaging>
  <modules>
    <module>ruoyi-admin</module>
    <module>ruoyi-alarm</module>
  </modules>
  <dependencyManagement>
    <dependencies>
    </dependencies>
  </dependencyManagement>
</project>
"""

# 内部模块 ruoyi-alarm：声明 <parent>，继承 version 3.8.6（自身不写 version）。
_ALARM_POM = """<project>
  <parent>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>3.8.6</version>
  </parent>
  <artifactId>ruoyi-alarm</artifactId>
</project>
"""

# ruoyi-admin：依赖 com.ruoyi:ruoyi-alarm，【未写 version】（round18 st-1 的原始缺陷）。
_ADMIN_POM = """<project>
  <parent>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>3.8.6</version>
  </parent>
  <artifactId>ruoyi-admin</artifactId>
  <dependencies>
    <dependency>
      <groupId>com.ruoyi</groupId>
      <artifactId>ruoyi-alarm</artifactId>
    </dependency>
  </dependencies>
</project>
"""


def _mkproj(tmp_path, files: dict[str, str]) -> str:
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


# ── 1. 闸门：reconcile 前，版本缺省的内部依赖被检出 ──────────────────────────
def test_gate_detects_missing_version_before_reconcile(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT, "ruoyi-alarm/pom.xml": _ALARM_POM, "ruoyi-admin/pom.xml": _ADMIN_POM,
    })
    missing = missing_intra_project_module_versions(proj)
    assert any("ruoyi-alarm" in m for m in missing), (
        f"应检出 ruoyi-admin 对 ruoyi-alarm 的版本缺省内部依赖，实得 {missing}"
    )


# ── 2. reconcile 补 root dependencyManagement 版本 → 缺版本可解析 ────────────
def test_reconcile_backfills_dependency_management_version(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT, "ruoyi-alarm/pom.xml": _ALARM_POM, "ruoyi-admin/pom.xml": _ADMIN_POM,
    })
    reconcile_workspace_manifests(proj)
    root_text = (tmp_path / "pom.xml").read_text()
    # root dependencyManagement 现声明 ruoyi-alarm 版本
    import re
    dm = re.search(r"<dependencyManagement>(.*?)</dependencyManagement>", root_text, re.S)
    assert dm, "dependencyManagement 块应仍在"
    assert "ruoyi-alarm" in dm.group(1) and "3.8.6" in dm.group(1), (
        f"root dependencyManagement 应补 ruoyi-alarm:3.8.6，实得:\n{dm.group(1)}"
    )
    # 闸门现应为空（版本已可解析）
    assert not missing_intra_project_module_versions(proj), "reconcile 后不应再有缺版本内部依赖"


# ── 3. 幂等：再跑一次不重复插入 ───────────────────────────────────────────
def test_reconcile_versions_idempotent(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT, "ruoyi-alarm/pom.xml": _ALARM_POM, "ruoyi-admin/pom.xml": _ADMIN_POM,
    })
    reconcile_workspace_manifests(proj)
    once = (tmp_path / "pom.xml").read_text()
    reconcile_workspace_manifests(proj)
    twice = (tmp_path / "pom.xml").read_text()
    assert once == twice, "版本对账应幂等（第二次无改动）"


# ── 4. 已有版本的内部依赖不误报、不重复 ──────────────────────────────────
def test_existing_version_not_flagged(tmp_path):
    admin_with_ver = _ADMIN_POM.replace(
        "<artifactId>ruoyi-alarm</artifactId>",
        "<artifactId>ruoyi-alarm</artifactId>\n      <version>3.8.6</version>",
    )
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT, "ruoyi-alarm/pom.xml": _ALARM_POM, "ruoyi-admin/pom.xml": admin_with_ver,
    })
    assert not missing_intra_project_module_versions(proj), "显式带 version 的内部依赖不应被判缺失"


# ── 5. 外部依赖（非本工程模块）不碰 ──────────────────────────────────────
def test_external_dependency_ignored(tmp_path):
    admin_ext = _ADMIN_POM.replace(
        "<groupId>com.ruoyi</groupId>\n      <artifactId>ruoyi-alarm</artifactId>",
        "<groupId>org.apache.commons</groupId>\n      <artifactId>commons-lang3</artifactId>",
    )
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT, "ruoyi-alarm/pom.xml": _ALARM_POM, "ruoyi-admin/pom.xml": admin_ext,
    })
    # commons-lang3 非内部模块 → 不判缺失（版本策略交给外部 BOM/用户，不臆造）
    missing = missing_intra_project_module_versions(proj)
    assert not any("commons-lang3" in m for m in missing), f"外部依赖不应被内部版本闸门管辖: {missing}"
    reconcile_workspace_manifests(proj)
    assert "commons-lang3" not in (tmp_path / "pom.xml").read_text(), "不应把外部依赖塞进 dependencyManagement"


# ── 6. 无 dependencyManagement 块时保守跳过 reconcile，但闸门仍报缺（fail-closed）──
def test_no_depmgmt_block_conservative(tmp_path):
    root_no_dm = _ROOT.replace(
        "  <dependencyManagement>\n    <dependencies>\n    </dependencies>\n  </dependencyManagement>\n",
        "",
    )
    proj = _mkproj(tmp_path, {
        "pom.xml": root_no_dm, "ruoyi-alarm/pom.xml": _ALARM_POM, "ruoyi-admin/pom.xml": _ADMIN_POM,
    })
    # 无 depMgmt 块：reconcile 不臆造结构（保守），但闸门仍须报缺 → 交付 fail-closed
    reconcile_workspace_manifests(proj)
    assert missing_intra_project_module_versions(proj), "无 depMgmt 块且内部依赖缺版本 → 闸门必须报缺(fail-closed)"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
