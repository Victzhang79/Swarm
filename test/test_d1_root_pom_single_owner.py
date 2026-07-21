"""D1 治本复现：plan 期 root pom 写权归一（收敛唯一 aggregator-owner）。

round18 P0-A 铁证：st-1(注册 ruoyi-alarm) 与 st-30(replan 补 dependencyManagement 版本) 都
【整段结构重写 root pom】的 <modules>/<dependencyManagement>。二者各自的结构重写无法 3-way
合并 → MERGE#2 apply_ok=False 畸形(重复闭标签+斩头 dependency 片段)，或 rebase 循环→escalate→FAILED。
两条都到不了 DELIVERED。

治本不变量：**root pom.xml 永远单写者**（收敛唯一 owner）。非首写者 demote 为 readable + 依赖 owner
（防环）。这样 MERGE 层根本没有两份结构重写可撞。安全性依据：根 <modules> 的成员注册由
`reconcile_workspace_manifests`(_reconcile_maven) 据磁盘 ground-truth 【确定性补齐】(L1/L2/交付三处
都跑)——demote 掉的写者的 <module> 登记不会丢；dependencyManagement 版本由 D2 的 reconcile 兜底。

本文件【先于实现】编写：对当前代码(保留双写者串行化)应 FAIL，D1 落地后 PASS。
"""
from __future__ import annotations

import subprocess

from swarm.brain.contract_utils import normalize_plan_scopes, resolve_plan_conflicts
from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, files: dict[str, str]) -> str:
    proj = tmp_path / "proj"
    proj.mkdir()
    for rel, content in files.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(proj, "init", "-q")
    _git(proj, "add", "-A")
    _git(proj, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return str(proj)


def _st(sid, *, writable=None, create=None, readable=None, depends=None):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(
            writable=writable or [], create_files=create or [], readable=readable or [],
        ),
        harness=TaskHarness(language="java"),
        depends_on=depends or [],
    )


# 已存在的多模块根 pom（含 <modules> 与 <dependencyManagement>，被两写者结构重写的对象）。
_ROOT_POM = (
    "<project>\n"
    "  <modules>\n"
    "    <module>ruoyi-admin</module>\n"
    "  </modules>\n"
    "  <dependencyManagement>\n"
    "    <dependencies>\n"
    "    </dependencies>\n"
    "  </dependencyManagement>\n"
    "</project>\n"
)


def _writers_of_root_pom(plan) -> list[str]:
    out = []
    for st in plan.subtasks:
        w = set(st.scope.writable or []) | set(st.scope.create_files or [])
        if "pom.xml" in w:
            out.append(st.id)
    return out


# ── 1. round18 现场：双 pom 写者(依赖序) → 收敛唯一 owner ──────────────────────
def test_round18_double_pom_writer_converges_to_single_owner(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM, "ruoyi-admin/pom.xml": "<project/>"})
    # st-1：脚手架，建 ruoyi-alarm 模块 pom + 写 root pom 注册它
    st1 = _st("st-1", create=["ruoyi-alarm/pom.xml", "ruoyi-alarm/src/A.java"],
              writable=["pom.xml"])
    # st-30：replan 补版本，建 sdk 模块 pom + 也写 root pom（依赖 st-1）
    st30 = _st("st-30", create=["ruoyi-alarm-sdk/pom.xml"], writable=["pom.xml"],
               depends=["st-1"])
    plan = TaskPlan(subtasks=[st1, st30])

    normalize_plan_scopes(plan, project_path=proj)

    writers = _writers_of_root_pom(plan)
    assert writers == ["st-1"], (
        f"root pom 必须收敛唯一 owner(拓扑首写者 st-1)，实际写者={writers}。"
        "双写者=P0-A 畸形/rebase 循环根因。"
    )
    st30_after = next(s for s in plan.subtasks if s.id == "st-30")
    assert "pom.xml" in (st30_after.scope.readable or []), "非 owner 应 demote 为 readable"
    # round29 A(c) 方向反正：st-30 是【脚手架】(建 ruoyi-alarm-sdk/pom.xml)，规范不变量=
    # 「注册后于脚手架」——owner(注册者 st-1) 依赖 scaffold(st-30)，旧的反向 demote 边
    # (st-30→st-1「注册就位先行」) 正是 d37a52a3 Child-module-does-not-exist 级联根因，须删。
    st1_after = next(s for s in plan.subtasks if s.id == "st-1")
    assert "st-30" in (st1_after.depends_on or []), "owner(注册者)应依赖 scaffold(注册后于脚手架)"
    assert "st-1" not in (st30_after.depends_on or []), "反向边(注册先行)必须被删，防 2-cycle"
    # 各自的【模块 pom】(不同文件)不受影响，仍各自创建 → 供 reconcile 据磁盘登记
    assert "ruoyi-alarm-sdk/pom.xml" in (st30_after.scope.create_files or [])


# ── 2. owner 恒登记全部新模块（即便 owner 预先存在，不再仅 unowned 时才补）──────
def test_owner_registers_all_new_modules(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st1 = _st("st-1", create=["ruoyi-alarm/pom.xml", "ruoyi-alarm/src/A.java"],
              writable=["pom.xml"])
    st30 = _st("st-30", create=["ruoyi-alarm-sdk/pom.xml"], writable=["pom.xml"],
               depends=["st-1"])
    plan = TaskPlan(subtasks=[st1, st30])
    normalize_plan_scopes(plan, project_path=proj)
    owner = next(s for s in plan.subtasks if s.id == "st-1")
    ac = " ".join(owner.acceptance_criteria or [])
    assert "ruoyi-alarm" in ac and "ruoyi-alarm-sdk" in ac, (
        f"owner 应登记全部新模块(含被 demote 写者的模块)，实际 acceptance={ac}"
    )


# ── 3. VALIDATE 硬阻双 root pom 写者（backstop，收敛后永不触发；触发即失败闭合）──
def test_validator_hard_blocks_double_root_pom_writer():
    # 直接构造绕过 normalize 的双写者 plan（模拟收敛失效的兜底）
    sts = [_st("st-1", writable=["pom.xml"], depends=[]),
           _st("st-2", writable=["pom.xml"], depends=["st-1"])]
    plan = TaskPlan(subtasks=sts)
    res = validate_plan_structure(plan)
    assert not res.valid, "根 pom 双写者(即便依赖序)必须硬失败——两份结构重写无法安全合并"
    assert any("pom.xml" in i for i in res.issues), f"issues 应点名 root pom: {res.issues}"


# ── 3b. #39-A 栈中立：非 Maven 根聚合清单双写者同样硬失败（settings.gradle/go.work/Cargo）──
def test_39a_non_maven_root_aggregator_double_writer_hard_fails():
    """#39-A 治本：此前只 pom.xml 硬失败，Gradle settings.gradle / Go go.work 的【依赖序】
    双写者只落 warn 逃过 backstop → include(...)/use 结构重写非加性=rebase 循环。栈中立铺开：
    根级聚合清单集统一硬失败（即便依赖序）。"""
    for manifest in ("settings.gradle", "settings.gradle.kts", "go.work", "Cargo.toml"):
        sts = [_st("st-1", writable=[manifest], depends=[]),
               _st("st-2", writable=[manifest], depends=["st-1"])]  # 依赖序（旧码只 warn）
        res = validate_plan_structure(TaskPlan(subtasks=sts))
        assert not res.valid, f"{manifest} 根聚合双写者(依赖序)必须硬失败，非仅 warn"
        assert any(manifest in i for i in res.issues), f"issues 应点名 {manifest}: {res.issues}"


def test_39a_member_manifest_not_treated_as_root_aggregator():
    """#39-A 边界：子目录同名清单（member Cargo.toml / 模块 build.gradle）不是【根】聚合，
    不吃根级单写者硬失败（沿用既有 module-writer 规则，依赖序放行）。"""
    sts = [_st("st-1", writable=["crates/foo/Cargo.toml"], depends=[]),
           _st("st-2", writable=["crates/foo/Cargo.toml"], depends=["st-1"])]
    res = validate_plan_structure(TaskPlan(subtasks=sts))
    # 依赖序 module 清单 → 不硬失败（走下方 warn 分支，valid 保持）
    assert res.valid, f"member 清单依赖序双写者不应硬失败: {res.issues}"


# ── 4. 收敛后 validator 通过（单 owner 无冲突）──────────────────────────────
def test_single_owner_passes_validator(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st1 = _st("st-1", create=["ruoyi-alarm/pom.xml"], writable=["pom.xml"])
    st30 = _st("st-30", create=["ruoyi-alarm-sdk/pom.xml"], writable=["pom.xml"],
               depends=["st-1"])
    plan = TaskPlan(subtasks=[st1, st30])
    normalize_plan_scopes(plan, project_path=proj)
    res = validate_plan_structure(plan)
    assert res.valid, f"收敛唯一 owner 后应通过校验，issues={res.issues}"


# ── 5. 防环：owner(拓扑首写者)反向已依赖被 demote 写者时不成环 ──────────────
def test_demote_cycle_guarded(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    # 列表序 [st-1, st-2] → first_writer=st-1；但 st-1 依赖 st-2（反向）。
    sts = [_st("st-1", writable=["pom.xml"], depends=["st-2"]),
           _st("st-2", writable=["pom.xml"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan, project_path=proj)
    res = validate_plan_structure(plan)
    assert "循环依赖" not in " ".join(res.issues), f"不应成环: {res.issues}"
    assert len(_writers_of_root_pom(plan)) == 1, "仍应收敛唯一 owner"


# ── 6. 无 project_path 路径(VALIDATE)也收敛唯一 owner（不依赖仓库感知）──────────
def test_converges_without_project_path():
    sts = [_st("st-1", writable=["pom.xml"]),
           _st("st-a", writable=["pom.xml"], depends=["st-1"]),
           _st("st-b", writable=["pom.xml"], depends=["st-1"])]
    plan = TaskPlan(subtasks=sts)
    normalize_plan_scopes(plan)  # 无 project_path
    assert _writers_of_root_pom(plan) == ["st-1"], "VALIDATE 路径也须收敛唯一 owner"
    res = validate_plan_structure(plan)
    assert res.valid, f"收敛后应通过: {res.issues}"


# ── 7. 全 pass 序列(resolve_plan_conflicts)端到端也收敛且校验通过 ──────────────
def test_resolve_plan_conflicts_end_to_end(tmp_path):
    proj = _repo(tmp_path, {"pom.xml": _ROOT_POM})
    st1 = _st("st-1", create=["ruoyi-alarm/pom.xml", "ruoyi-alarm/src/A.java"],
              writable=["pom.xml"])
    st30 = _st("st-30", create=["ruoyi-alarm-sdk/pom.xml"], writable=["pom.xml"],
               depends=["st-1"])
    plan = TaskPlan(subtasks=[st1, st30])
    resolve_plan_conflicts(plan, project_path=proj)
    assert len(_writers_of_root_pom(plan)) == 1
    res = validate_plan_structure(plan)
    assert res.valid, f"resolve_plan_conflicts 后应通过: {res.issues}"


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
