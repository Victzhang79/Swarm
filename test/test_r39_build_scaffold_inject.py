#!/usr/bin/env python3
"""R39-4（round39 治本批）—— 规则5 pom owner 落空 → 确定性脚手架子任务注入。

取证：round39 三轮 VALIDATE 各 6 模块规则5 WARNING（55 artifacts 落空）无人消费
（#30② 同病）；脚手架目前只靠 prompts.py:77-78 叮嘱 LLM，LLM 没听=落空。
治本（零 LLM）：unclaimed_contract_deps 命中的模块，确定性注入"建/补该模块构建
文件"的脚手架子任务——契约 dependencies 全集随子任务 contract 落地（写代码的
子任务碰不到构建文件，缺一个依赖=整模块编译失败）；同模块其余子任务 depends_on
脚手架（先有构建文件再编译）。构建文件路径沿用规则5 自身口径（<module>/pom.xml，
Maven 专属是既有产品决策，round24 A2 先例）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import (  # noqa: E402
    inject_build_scaffold_subtasks,
    unclaimed_contract_deps,
)
from swarm.brain.plan_validator import validate_plan_structure  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    TaskPlan,
)


def _st(sid, desc="", writable=None, create=None):
    return SubTask(id=sid, description=desc or f"task {sid}",
                   difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=writable or [], create_files=create or []))


def _plan_two_modules():
    """round39 真场景缩影：两物理模块、零 pom owner（6 模块全落空的最小版）。

    注意规则5 的 A5 归并早退：恰好一个 pom owner 时视为单物理模块恒空——
    零 owner / ≥2 owner 才进落空判定。"""
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod-a/src/main/java/A.java"]),
        _st("st-2", create=["mod-b/src/main/java/B.java"]),
        _st("st-3", create=["mod-b/src/main/java/C.java"]),
    ], parallel_groups=[["st-1"], ["st-2", "st-3"]])
    plan.shared_contract = {"dependencies": [
        {"module": "mod-a", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "mod-b", "artifacts": ["org.projectlombok:lombok",
                                          "org.springframework:spring-context"]},
    ]}
    return plan


def test_inject_creates_scaffold_for_unclaimed_module(tmp_path):
    plan = _plan_two_modules()
    assert len(unclaimed_contract_deps(plan)) == 2, "前置：两模块规则5 全落空"
    # R41 复核 F5 语义更新：CREATE 需【确证基线无 pom】（传真实 project_path）；
    # project_path 未知时保守 MODIFY（见 test_r41 F5 用例），防 clobber 基线 pom
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    assert {e["module"] for e in injected} == {"mod-a", "mod-b"}
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    sc_st = next(st for st in plan.subtasks if st.id == sid)
    assert "mod-b/pom.xml" in sc_st.scope.create_files, "确证基线无 pom → create_files"
    assert sc_st.contract.get("dependencies"), "契约 dependencies 全集随脚手架落地"
    arts = sc_st.contract["dependencies"][0]["artifacts"]
    assert "org.springframework:spring-context" in arts
    # 注入后规则5 清零（治的就是"WARNING 无人消费"）
    assert not unclaimed_contract_deps(plan)


def test_module_subtasks_depend_on_scaffold():
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan)
    sid_b = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st2 = next(st for st in plan.subtasks if st.id == "st-2")
    st3 = next(st for st in plan.subtasks if st.id == "st-3")
    assert sid_b in st2.depends_on and sid_b in st3.depends_on, (
        "同模块子任务先等构建文件落地再编译")
    st1 = next(st for st in plan.subtasks if st.id == "st-1")
    assert sid_b not in st1.depends_on, "别的模块不受影响（不过度串行）"
    sc_st = next(st for st in plan.subtasks if st.id == sid_b)
    assert not sc_st.depends_on, "脚手架无上游依赖=不可能成环"


def test_plan_structure_stays_valid_after_inject():
    plan = _plan_two_modules()
    inject_build_scaffold_subtasks(plan)
    r = validate_plan_structure(plan)
    assert r.valid, f"注入后结构校验必须通过（parallel_groups 完整性等）: {r.issues}"


def test_existing_pom_goes_writable(tmp_path):
    proj = tmp_path / "proj"
    (proj / "mod-b").mkdir(parents=True)
    (proj / "mod-b/pom.xml").write_text("<project/>", encoding="utf-8")
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, project_path=str(proj))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    sc_st = next(st for st in plan.subtasks if st.id == sid)
    assert "mod-b/pom.xml" in sc_st.scope.writable, "基线已有 pom → writable 修改"
    assert "mod-b/pom.xml" not in sc_st.scope.create_files


def test_idempotent_and_noop_when_clean():
    plan = _plan_two_modules()
    inject_build_scaffold_subtasks(plan)
    n = len(plan.subtasks)
    assert inject_build_scaffold_subtasks(plan) == [], "二次注入无事可做"
    assert len(plan.subtasks) == n
    # 单 pom owner 场景（A5 归并：规则5 恒空）→ 不注入
    clean = TaskPlan(subtasks=[
        _st("st-1", create=["mod-x/pom.xml", "mod-x/src/A.java"])],
        parallel_groups=[["st-1"]])
    clean.shared_contract = {"dependencies": [
        {"module": "mod-x", "artifacts": ["g:a"]}]}
    assert unclaimed_contract_deps(clean) == [], "前置：A5 归并恒空"
    assert inject_build_scaffold_subtasks(clean) == []


def test_r45_pom_template_embedded_when_root_pom_parseable(tmp_path):
    """R45-2：根 pom 可解析时 scaffold description 内嵌确定性 pom 模板（小模型抄不编）。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.7.8</version><packaging>pom</packaging></project>", "utf-8")
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st = next(s for s in plan.subtasks if s.id == sid)
    d = st.description
    assert "<parent>" in d and "com.ruoyi" in d and "4.7.8" in d, "parent GAV 来自根 pom"
    assert "<artifactId>mod-b</artifactId>" in d
    assert "spring-context" in d, "契约 artifacts 展开成 <dependency>"
    import xml.etree.ElementTree as ET
    xml = d.split("```xml\n", 1)[1].split("\n```", 1)[0]
    ET.fromstring(xml)  # 模板必须是合法 XML


def test_r45_pom_template_absent_without_root_pom(tmp_path):
    """根 pom 缺失 → 模板留空退回旧行为（不假装精确）。"""
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st = next(s for s in plan.subtasks if s.id == sid)
    assert "权威 pom 模板" not in st.description


def test_r45_f1_inherited_gav_root_pom_fails_open(tmp_path):
    """复核 F1：根 pom 继承 GAV（无自身 groupId/version）→ 模板必须留空，
    绝不用 dependencies 区块里的坐标拼幽灵 parent。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<parent><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-parent</artifactId>"
        "<version>3.2.0</version></parent>"
        "<artifactId>acme-app</artifactId>"
        "<dependencies><dependency><groupId>com.fasterxml.jackson</groupId>"
        "<artifactId>jackson-databind</artifactId><version>2.15.2</version>"
        "</dependency></dependencies></project>", "utf-8")
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st = next(s for s in plan.subtasks if s.id == sid)
    assert "权威 pom 模板" not in st.description
    assert "jackson" not in st.description, "依赖区坐标绝不冒充工程 GAV"


def test_r45_f2_commented_coordinates_ignored(tmp_path):
    """复核 F2：注释里的历史坐标不得赢过真坐标。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<!-- <groupId>com.legacy</groupId><version>0.9</version> -->"
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.7.8</version></project>", "utf-8")
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st = next(s for s in plan.subtasks if s.id == sid)
    assert "com.ruoyi" in st.description and "com.legacy" not in st.description


def test_r45_f3_modify_case_gets_merge_snippets_not_full_template(tmp_path):
    """复核 F3：既有 pom（MODIFY）只给依赖片段+并入措辞，绝不给"原样写入"全模板。"""
    proj = tmp_path / "proj"
    (proj / "mod-b").mkdir(parents=True)
    (proj / "mod-b/pom.xml").write_text("<project/>", "utf-8")
    (proj / "pom.xml").write_text(
        "<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.7.8</version></project>", "utf-8")
    plan = _plan_two_modules()
    injected = inject_build_scaffold_subtasks(plan, str(proj))
    sid = next(e["subtask_id"] for e in injected if e["module"] == "mod-b")
    st = next(s for s in plan.subtasks if s.id == sid)
    assert "权威 pom 模板" not in st.description and "原样写入" not in st.description
    assert "并入" in st.description and "spring-context" in st.description
    assert "绝不整体替换" in st.description


def test_r47_bare_artifact_group_resolved_from_baseline(tmp_path):
    """R47-2：裸 artifact 的 groupId 从基线 poms 解析，绝不回退工程 groupId。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.7.8</version><packaging>pom</packaging>"
        "<dependencyManagement><dependencies><dependency>"
        "<groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId>"
        "<version>2.5.15</version></dependency>"
        "</dependencies></dependencyManagement></project>", "utf-8")
    from swarm.brain.contract_utils import _deterministic_pom_template
    tpl = _deterministic_pom_template(
        "mod-b", ["spring-boot-starter-web"], str(tmp_path))
    assert "<groupId>org.springframework.boot</groupId>" in tpl
    assert "com.ruoyi</groupId>\n            <artifactId>spring-boot-starter-web" \
        not in tpl.replace("    ", " ")
    # 工程 groupId 只出现在 parent 块
    assert tpl.count("com.ruoyi") == 1


def test_r47_unresolvable_bare_artifact_omitted(tmp_path):
    """R47-2：基线解析不到的裸 artifact 从模板省略（缺依赖可修，伪造坐标是毒药）。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.7.8</version><packaging>pom</packaging></project>", "utf-8")
    from swarm.brain.contract_utils import _deterministic_pom_template
    tpl = _deterministic_pom_template(
        "mod-b", ["totally-unknown-artifact", "org.projectlombok:lombok"], str(tmp_path))
    assert "totally-unknown-artifact" not in tpl
    assert "<artifactId>lombok</artifactId>" in tpl, "显式 g:a 照常展开"


def test_r47_baseline_group_from_sibling_module_pom(tmp_path):
    """R47-2：root pom 无此依赖时，兄弟模块 pom 的依赖块也算基线证据。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.7.8</version><packaging>pom</packaging></project>", "utf-8")
    (tmp_path / "ruoyi-common").mkdir()
    (tmp_path / "ruoyi-common" / "pom.xml").write_text(
        "<project><parent><groupId>com.ruoyi</groupId></parent>"
        "<artifactId>ruoyi-common</artifactId><dependencies><dependency>"
        "<groupId>cn.hutool</groupId><artifactId>hutool-all</artifactId>"
        "</dependency></dependencies></project>", "utf-8")
    from swarm.brain.contract_utils import _deterministic_pom_template
    tpl = _deterministic_pom_template("mod-b", ["hutool-all"], str(tmp_path))
    assert "<groupId>cn.hutool</groupId>" in tpl


def test_r47_f1_poisoned_sibling_pom_rejected(tmp_path):
    """复核 F1（真树复现级）：残留毒 pom 声明 com.ruoyi:spring-boot-starter-web ——
    工程 groupId + 非内部模块 artifact = 伪造，无论证据来自哪都拒绝（省略该依赖）。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version><packaging>pom</packaging>"
        "<modules><module>ruoyi-common</module></modules></project>", "utf-8")
    (tmp_path / "alarm-notify-api").mkdir()
    (tmp_path / "alarm-notify-api" / "pom.xml").write_text(
        "<project><parent><groupId>com.ruoyi</groupId></parent>"
        "<artifactId>alarm-notify-api</artifactId><dependencies><dependency>"
        "<groupId>com.ruoyi</groupId><artifactId>spring-boot-starter-web</artifactId>"
        "</dependency></dependencies></project>", "utf-8")
    from swarm.brain.contract_utils import _dep_group_from_baseline
    assert _dep_group_from_baseline(str(tmp_path), "spring-boot-starter-web") is None
    # 真内部模块（root <modules> 登记）→ 工程 groupId 合法
    assert _dep_group_from_baseline(str(tmp_path), "ruoyi-common") == "com.ruoyi"


def test_r47_f1_conflicting_third_party_evidence_rejected(tmp_path):
    """互斥第三方证据 → 存疑弃用；唯一第三方证据胜过毒证据。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version><packaging>pom</packaging></project>", "utf-8")
    (tmp_path / "bad-mod").mkdir()
    (tmp_path / "bad-mod" / "pom.xml").write_text(
        "<project><parent><groupId>com.ruoyi</groupId></parent>"
        "<artifactId>bad-mod</artifactId><dependencies><dependency>"
        "<groupId>com.ruoyi</groupId><artifactId>hutool-all</artifactId>"
        "</dependency></dependencies></project>", "utf-8")
    (tmp_path / "good-mod").mkdir()
    (tmp_path / "good-mod" / "pom.xml").write_text(
        "<project><parent><groupId>com.ruoyi</groupId></parent>"
        "<artifactId>good-mod</artifactId><dependencies><dependency>"
        "<groupId>cn.hutool</groupId><artifactId>hutool-all</artifactId>"
        "</dependency></dependencies></project>", "utf-8")
    from swarm.brain.contract_utils import _dep_group_from_baseline
    # 毒证据(工程 groupId)在场时，唯一真第三方证据仍胜出
    assert _dep_group_from_baseline(str(tmp_path), "hutool-all") == "cn.hutool"


def test_r47_f2_exclusion_artifact_not_treated_as_dep(tmp_path):
    """复核 F2：只以 exclusion 形式出现的 artifact 不得错配外层 groupId。"""
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project>'
        "<groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version><packaging>pom</packaging>"
        "<dependencyManagement><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "<version>5.3.39</version>"
        "<exclusions><exclusion><groupId>commons-logging</groupId>"
        "<artifactId>commons-logging</artifactId></exclusion></exclusions>"
        "</dependency></dependencies></dependencyManagement></project>", "utf-8")
    from swarm.brain.contract_utils import _dep_group_from_baseline
    assert _dep_group_from_baseline(str(tmp_path), "commons-logging") is None
