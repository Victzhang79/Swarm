"""T5（round63）：pom 模板含内部（基线/兄弟）模块依赖——确定性推导，不靠契约 LLM 自觉。

round63 死因（register T5 调查结论）：模板 <dependencies> 唯一来源=契约 LLM 自声明的第三方
artifacts；契约从不产内部模块依赖（st-5 描述文字明说要 ruoyi-common/ruoyi-framework，
"权威模板"XML 里却没有，worker 被指示原样写入）→ 首波 30+ 次
"程序包 com.ruoyi.common.core.domain 不存在"。plan 里推导证据现成：ruoyi-alarm 子任务
readable→ruoyi-common code 文件 108 次，从无人消费。

治本：derive_internal_module_deps（栈中立证据面=模块子任务的跨模块 readable code 文件；
Maven 注入层过滤=须有 pom、packaging 非 pom/war、非 spring-boot 可执行件；plan 新兄弟
一向依赖=显式 group:artifact:${project.version}，互指成环=双向跳过+WARNING）→ 两个模板
注入点（scaffold + R58-3 owner 嵌入）合并进 artifacts，交既有 resolve_scaffold_artifacts。

另：plan_finisher"Task4 coherence 闸待接管"是过时前瞻注释（闸已在 cc7be64 落地接线），
本轮回填措辞防再误读。
"""
from __future__ import annotations

import logging

from swarm.brain.contract_utils import (
    derive_internal_module_deps,
    inject_build_scaffold_subtasks,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

_ROOT_POM = (
    '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
    "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
    "<packaging>pom</packaging>"
    "<modules><module>ruoyi-common</module></modules></project>")

_COMMON_POM = (
    '<?xml version="1.0"?><project>'
    "<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
    "<version>4.8.3</version></parent>"
    "<artifactId>ruoyi-common</artifactId></project>")


def _st(sid, *, create=None, writable=None, readable=None, desc=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(create_files=create or [], writable=writable or [],
                                   readable=readable or []))


def _plan(subs, deps_entries):
    plan = TaskPlan(subtasks=subs, parallel_groups=[[s.id for s in subs]])
    plan.shared_contract = {"dependencies": deps_entries}
    return plan


def _mk_repo(tmp_path, extra=None):
    (tmp_path / "pom.xml").write_text(_ROOT_POM, encoding="utf-8")
    (tmp_path / "ruoyi-common").mkdir()
    (tmp_path / "ruoyi-common" / "pom.xml").write_text(_COMMON_POM, encoding="utf-8")
    for name, pom_text in (extra or {}).items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "pom.xml").write_text(pom_text, encoding="utf-8")
    return str(tmp_path)


_BASE_ENTITY = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/BaseEntity.java"


# ───────────────────────── derive_internal_module_deps ─────────────────────────

def test_derive_baseline_lib_from_readable_evidence(tmp_path):
    """★round63 st-5 真死因形态★：模块子任务 readable 指向基线 lib 模块的 code 文件
    → 推导出该模块为内部依赖。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-6", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"],
             readable=[_BASE_ENTITY])],
        [{"module": "ruoyi-alarm", "artifacts": ["org.projectlombok:lombok"]}])
    derived = derive_internal_module_deps(plan, {"ruoyi-alarm": "ruoyi-alarm"}, proj)
    assert "ruoyi-common" in (derived.get("ruoyi-alarm") or [])


def test_derive_skips_executable_and_pom_packaging(tmp_path):
    """packaging=pom（聚合父）与 spring-boot 可执行件（repackage 后不可被依赖）绝不注入。"""
    admin_pom = ('<?xml version="1.0"?><project><artifactId>ruoyi-admin</artifactId>'
                 "<build><plugins><plugin>"
                 "<groupId>org.springframework.boot</groupId>"
                 "<artifactId>spring-boot-maven-plugin</artifactId>"
                 "</plugin></plugins></build></project>")
    agg_pom = ('<?xml version="1.0"?><project><artifactId>agg</artifactId>'
               "<packaging>pom</packaging></project>")
    proj = _mk_repo(tmp_path, extra={"ruoyi-admin": admin_pom, "agg": agg_pom})
    plan = _plan(
        [_st("st-1", create=["ruoyi-alarm/src/main/java/A.java"],
             readable=["ruoyi-admin/src/main/java/com/ruoyi/web/SysLoginController.java",
                       "agg/some/Code.java"])],
        [{"module": "ruoyi-alarm", "artifacts": []}])
    derived = derive_internal_module_deps(plan, {"ruoyi-alarm": "ruoyi-alarm"}, proj)
    got = derived.get("ruoyi-alarm") or []
    assert not any("ruoyi-admin" in d for d in got), "可执行件绝不可被依赖"
    assert not any("agg" in d for d in got), "packaging=pom 绝不可被依赖"


def test_derive_ignores_non_code_and_src_layout(tmp_path):
    """非 code readable（清单/资源）与 src 布局顶段不构成依赖证据。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-1", create=["ruoyi-alarm/src/main/java/A.java"],
             readable=["ruoyi-common/pom.xml", "src/main/java/Local.java"])],
        [{"module": "ruoyi-alarm", "artifacts": []}])
    derived = derive_internal_module_deps(plan, {"ruoyi-alarm": "ruoyi-alarm"}, proj)
    assert not derived.get("ruoyi-alarm")


def test_derive_plan_sibling_one_way_explicit_coordinate(tmp_path):
    """plan 新兄弟模块（磁盘尚无 pom）单向消费 → 显式 group:artifact:${project.version}。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-a", create=["alarm-interface/src/main/java/com/x/IChannel.java"]),
         _st("st-b", create=["ruoyi-alarm/src/main/java/com/x/Engine.java"],
             readable=["alarm-interface/src/main/java/com/x/IChannel.java"])],
        [{"module": "ruoyi-alarm", "artifacts": []},
         {"module": "alarm-interface", "artifacts": []}])
    dirs = {"ruoyi-alarm": "ruoyi-alarm", "alarm-interface": "alarm-interface"}
    derived = derive_internal_module_deps(plan, dirs, proj)
    assert "com.ruoyi:alarm-interface:${project.version}" in (derived.get("ruoyi-alarm") or [])
    assert not derived.get("alarm-interface"), "单向消费绝不反向加依赖"


def test_derive_mutual_siblings_skipped_with_warning(tmp_path, caplog):
    """plan 兄弟互指（A↔B 互相 readable）＝会成环 → 双向都不注入 + WARNING。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-a", create=["mod-a/src/main/java/A.java"],
             readable=["mod-b/src/main/java/B.java"]),
         _st("st-b", create=["mod-b/src/main/java/B.java"],
             readable=["mod-a/src/main/java/A.java"])],
        [{"module": "mod-a", "artifacts": []}, {"module": "mod-b", "artifacts": []}])
    dirs = {"mod-a": "mod-a", "mod-b": "mod-b"}
    with caplog.at_level(logging.WARNING):
        derived = derive_internal_module_deps(plan, dirs, proj)
    assert not any("mod-b" in d for d in derived.get("mod-a") or [])
    assert not any("mod-a" in d for d in derived.get("mod-b") or [])
    assert any("T5" in r.message for r in caplog.records)


def test_derive_failopen_no_project_path():
    plan = _plan([_st("st-1", create=["m/src/A.java"])], [{"module": "m", "artifacts": []}])
    assert derive_internal_module_deps(plan, {"m": "m"}, None) == {}


# ───────────────────────── 模板注入集成（round63 死型锁） ─────────────────────────

def test_owner_template_includes_baseline_dep(tmp_path):
    """★round63 st-5 死型端到端★：子任务认领新模块 pom（R58-3 owner 路径），readable 证据
    指向 ruoyi-common → 注入的权威模板必须含 ruoyi-common 依赖（${project.version}）。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-5", create=["ruoyi-alarm/pom.xml",
                             "ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"],
             readable=[_BASE_ENTITY])],
        [{"module": "ruoyi-alarm", "artifacts": ["org.projectlombok:lombok"]}])
    inject_build_scaffold_subtasks(plan, proj)
    st5 = next(s for s in plan.subtasks if s.id == "st-5")
    assert "权威 pom 模板" in st5.description
    assert "<artifactId>ruoyi-common</artifactId>" in st5.description, \
        "round63 死因：模板缺基线模块依赖 → BaseEntity 找不到"


def test_scaffold_template_includes_baseline_dep(tmp_path):
    """无人认领路径（st-scaffold-*）同样必须带推导出的内部依赖。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-6", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"],
             readable=[_BASE_ENTITY])],
        [{"module": "ruoyi-alarm", "artifacts": ["org.projectlombok:lombok"]}])
    inject_build_scaffold_subtasks(plan, proj)
    scaf = next((s for s in plan.subtasks if s.id == "st-scaffold-ruoyi-alarm"), None)
    assert scaf is not None
    assert "<artifactId>ruoyi-common</artifactId>" in scaf.description


def test_no_duplicate_when_contract_already_lists(tmp_path):
    """契约已列 ruoyi-common → 不重复注入（模板里恰一个依赖块）。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-5", create=["ruoyi-alarm/pom.xml",
                             "ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"],
             readable=[_BASE_ENTITY])],
        [{"module": "ruoyi-alarm", "artifacts": ["com.ruoyi:ruoyi-common"]}])
    inject_build_scaffold_subtasks(plan, proj)
    st5 = next(s for s in plan.subtasks if s.id == "st-5")
    assert st5.description.count("<artifactId>ruoyi-common</artifactId>") == 1


# ───────────────────────── 对抗复核回归锁 ─────────────────────────

def test_empty_artifacts_module_still_gets_scaffold_with_internal_dep(tmp_path):
    """★hunter#F1 HIGH（round63 死型本体）★：契约条目 artifacts 为空（模块只需内部基线库）
    会被 unclaimed_contract_deps 剪掉 → 修复前推导结果无处注入、模块 pom 无人建。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-6", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"],
             readable=[_BASE_ENTITY])],
        [{"module": "ruoyi-alarm", "artifacts": []}])
    inject_build_scaffold_subtasks(plan, proj)
    scaf = next((s for s in plan.subtasks if s.id == "st-scaffold-ruoyi-alarm"), None)
    assert scaf is not None, "零 artifacts 但有内部依赖证据的模块必须仍有脚手架出口"
    assert "<artifactId>ruoyi-common</artifactId>" in scaf.description


def test_sibling_without_root_gav_not_bare_name(tmp_path, caplog):
    """★hunter#F2 CRITICAL★：根 pom 继承 GAV（无字面 version）→ 新兄弟依赖绝不退化裸名
    （裸名会被 Central 反查解析成不相干真实构件=伪造坐标）→ 跳过 + WARNING。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<parent><groupId>org.ext</groupId><artifactId>corp-parent</artifactId>"
        "<version>1.0</version></parent>"
        "<artifactId>ruoyi</artifactId><packaging>pom</packaging></project>",
        encoding="utf-8")
    plan = _plan(
        [_st("st-a", create=["alarm-interface/src/main/java/com/x/IChannel.java"]),
         _st("st-b", create=["ruoyi-alarm/src/main/java/com/x/Engine.java"],
             readable=["alarm-interface/src/main/java/com/x/IChannel.java"])],
        [{"module": "ruoyi-alarm", "artifacts": []},
         {"module": "alarm-interface", "artifacts": []}])
    dirs = {"ruoyi-alarm": "ruoyi-alarm", "alarm-interface": "alarm-interface"}
    with caplog.at_level(logging.WARNING):
        derived = derive_internal_module_deps(plan, dirs, str(tmp_path))
    got = derived.get("ruoyi-alarm") or []
    assert not any("alarm-interface" in d for d in got), \
        "根 GAV 不可解析时绝不产出裸名/半截坐标"
    assert any("R47-2" in r.message for r in caplog.records)


def test_baseline_pom_read_failure_warns(tmp_path, caplog, monkeypatch):
    """hunter#F3：pom 读取 OSError ≠ 确认不可依赖 → 必须 WARNING 留痕（不与设计内过滤混同）。"""
    from pathlib import Path as _P

    from swarm.brain.contract_utils import _baseline_module_artifact
    proj = _mk_repo(tmp_path)

    def _boom(self, *a, **k):
        raise OSError("transient io")
    monkeypatch.setattr(_P, "read_text", _boom)
    with caplog.at_level(logging.WARNING):
        assert _baseline_module_artifact(_P(proj), "ruoyi-common") is None
    assert any("读取失败" in r.message for r in caplog.records)


def test_owner_mod_longest_prefix_nested_dirs(tmp_path):
    """复核 MED：嵌套模块目录（外层是内层的路径前缀）→ 证据归最具体（最长）目录的模块。"""
    proj = _mk_repo(tmp_path)
    plan = _plan(
        [_st("st-outer", create=["mods/src/main/java/Outer.java"]),
         _st("st-inner", create=["mods/alarm/src/main/java/Inner.java"]),
         _st("st-c", create=["ruoyi-alarm/src/main/java/C.java"],
             readable=["mods/alarm/src/main/java/Inner.java"])],
        [{"module": "outer", "artifacts": []}, {"module": "inner", "artifacts": []},
         {"module": "ruoyi-alarm", "artifacts": []}])
    dirs = {"outer": "mods", "inner": "mods/alarm", "ruoyi-alarm": "ruoyi-alarm"}
    derived = derive_internal_module_deps(plan, dirs, proj)
    got = derived.get("ruoyi-alarm") or []
    assert any(":inner:" in d for d in got), f"应归内层模块 inner，实得 {got}"
    assert not any(":outer:" in d for d in got), "绝不误归外层前缀模块"


def test_plan_module_on_existing_baseline_dir_uses_real_artifactid(tmp_path):
    """复核盲区（R58-1 混合形态）：契约模块落在**基线既有目录**（磁盘已有 lib pom）→
    依赖用该 pom 的真 artifactId，不用契约模块名。"""
    lib_pom = ('<?xml version="1.0"?><project>'
               "<parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
               "<version>4.8.3</version></parent>"
               "<artifactId>ruoyi-lib</artifactId></project>")
    proj = _mk_repo(tmp_path, extra={"ruoyi-lib": lib_pom})
    plan = _plan(
        [_st("st-a", writable=["ruoyi-lib/src/main/java/com/x/Util.java"]),
         _st("st-b", create=["ruoyi-alarm/src/main/java/com/x/Engine.java"],
             readable=["ruoyi-lib/src/main/java/com/x/Util.java"])],
        [{"module": "alarm-lib", "artifacts": []}, {"module": "ruoyi-alarm", "artifacts": []}])
    dirs = {"alarm-lib": "ruoyi-lib", "ruoyi-alarm": "ruoyi-alarm"}
    derived = derive_internal_module_deps(plan, dirs, proj)
    assert "ruoyi-lib" in (derived.get("ruoyi-alarm") or [])


def test_merge_internal_deps_dedup_table():
    """复核盲区：_merge_internal_deps 去重键=artifactId，显式/裸名冒号数不对称也要去重。"""
    from swarm.brain.contract_utils import _merge_internal_deps
    assert _merge_internal_deps(["com.ruoyi:ruoyi-common:1.0"], ["ruoyi-common"]) == \
        ["com.ruoyi:ruoyi-common:1.0"]
    assert _merge_internal_deps(["ruoyi-common"], ["com.ruoyi:ruoyi-common"]) == \
        ["ruoyi-common"]
    assert _merge_internal_deps([], ["a", "g:a", "b"]) == ["a", "b"]
    assert _merge_internal_deps(["x:y"], []) == ["x:y"]
