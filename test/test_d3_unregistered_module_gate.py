"""D3 治本复现：未注册模块 false-PASS 双穿透（Fix E）。

round18 §2③ 铁证：`_scope_maven_command` 找不到 -pl hit（改动落在【未注册进 root <modules>】的
孤儿模块）→ `return command` 原样整仓 `mvn compile` → 整仓只编 root <modules> 里的模块，
【静默跳过】未注册模块的 .java → L1 假 PASS；VERIFY_L2 也整仓 → L1+L2 双双放行未编译代码。

治本（fail-closed，通用 Maven）：改动落在【有自己 pom.xml 但未注册进 reactor】的模块时，
显式把它并进 -pl → `mvn` 报 "Could not find the selected project in the reactor"（暴露未注册），
而非静默整仓放行。真·根级文件（无所属模块 pom）仍走整仓 fallback（不误伤）。

本文件【先于实现】编写。
"""
from __future__ import annotations

from swarm.worker.l1_pipeline import _scope_maven_command

_ROOT_ONLY_ADMIN = """<project>
  <modules>
    <module>ruoyi-admin</module>
  </modules>
</project>
"""


def _mkproj(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


# ── 1. 孤儿模块(有 pom 未注册)改动 → fail-closed 显式 -pl(不静默整仓) ──────────
def test_unregistered_module_fails_closed(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT_ONLY_ADMIN,
        "ruoyi-admin/pom.xml": "<project/>",
        "ruoyi-alarm-sdk/pom.xml": "<project/>",  # 有 pom 但未注册进 root <modules>
    })
    out = _scope_maven_command(
        "mvn -q compile", proj,
        ["ruoyi-alarm-sdk/src/main/java/com/ruoyi/sdk/X.java"],
    )
    assert out != "mvn -q compile", "未注册模块改动不得静默整仓放行(假 PASS)"
    assert "-pl" in out and "ruoyi-alarm-sdk" in out, (
        f"应显式 -pl 孤儿模块(mvn 会报 not found in reactor,fail-closed)，实得 {out!r}"
    )


# ── 2. 已注册模块改动 → 正常 -pl 收窄(不回归) ──────────────────────────────
def test_registered_module_scopes_normally(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT_ONLY_ADMIN, "ruoyi-admin/pom.xml": "<project/>",
    })
    out = _scope_maven_command(
        "mvn -q compile", proj, ["ruoyi-admin/src/main/java/A.java"],
    )
    assert "-pl ruoyi-admin" in out, f"已注册模块应 -pl 收窄，实得 {out!r}"


# ── 3. 真·根级文件(无所属模块 pom) → 整仓 fallback 正确(不误伤) ──────────────
def test_root_level_file_whole_reactor(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT_ONLY_ADMIN, "ruoyi-admin/pom.xml": "<project/>",
    })
    out = _scope_maven_command("mvn -q compile", proj, ["README.md", "pom.xml"])
    assert out == "mvn -q compile", f"根级文件无所属模块 → 整仓 fallback 正确，实得 {out!r}"


# ── 4. 混合：已注册 + 孤儿 → 两者都进 -pl（孤儿不被静默漏编）──────────────────
def test_mixed_registered_and_orphan(tmp_path):
    proj = _mkproj(tmp_path, {
        "pom.xml": _ROOT_ONLY_ADMIN,
        "ruoyi-admin/pom.xml": "<project/>",
        "ruoyi-alarm-sdk/pom.xml": "<project/>",
    })
    out = _scope_maven_command(
        "mvn -q compile", proj,
        ["ruoyi-admin/src/A.java", "ruoyi-alarm-sdk/src/B.java"],
    )
    assert "ruoyi-admin" in out and "ruoyi-alarm-sdk" in out, (
        f"混合改动时孤儿模块也须进 -pl，不得只编已注册的而漏孤儿，实得 {out!r}"
    )


# ── 5. 单模块项目(根 pom 无 <modules>) → 不 scope(向后兼容) ──────────────────
def test_single_module_project_unchanged(tmp_path):
    proj = _mkproj(tmp_path, {"pom.xml": "<project/>"})
    out = _scope_maven_command("mvn -q compile", proj, ["src/main/java/A.java"])
    assert out == "mvn -q compile", "单模块项目无 reactor → 不 scope"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
