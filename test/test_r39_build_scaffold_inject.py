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

import pytest

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


@pytest.fixture(autouse=True)
def _stub_maven_registry(monkeypatch):
    """R53-1：坐标解析确定性打桩（单测禁联网，见 conftest SWARM_MAVEN_LOOKUP=0）。

    语义变更说明（不是把旧锁改绿，是旧锁锁的行为已被实测证明致死）：
    模板过去对"父级管不到又查不到版本"的依赖**照样不带版本写进 pom**——round51/52/53 三轮
    实锤，这会让 Maven 在 **pom 解析期**就炸（`'dependencies.dependency.version' … is missing`），
    整棵 reactor 读不出、全体 worker 构建闸 BLOCKED。现在：受管→不写版本；不受管→写解析到的
    显式版本；解析不到→如实丢弃（worker 的 L1 防线④会按真实 import 反查坐标补回）。
    """
    from swarm.brain import maven_registry as mr
    vers = {
        ("org.springframework", "spring-context"): "5.3.39",
        ("org.projectlombok", "lombok"): "1.18.34",
        ("cn.hutool", "hutool-all"): "5.8.47",
    }
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: vers.get((g, a)))
    monkeypatch.setattr(mr, "registry_group_for",
                        lambda a: {"hutool-all": "cn.hutool"}.get(a))
    mr._http_cache.clear()


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


# ── R57-1：契约里的模块名必须有【独立证据】才允许在磁盘上造出模块 ──────────────

def test_scaffold_refuses_module_names_with_no_code_evidence(tmp_path, caplog):
    """★R57-1 P0（round57 实锤）★ LLM 把 schema 占位符抄成模块名 → 脚手架凭空造出垃圾模块。

    实锤：契约里出现 `{"module": "module", ...}` 与 `{"module": "artifacts", ...}`
    （真实模块只有 alarm-*）。旧实现对模块名**零取证**，无条件建 `module/pom.xml`、
    `artifacts/pom.xml` → 真的在磁盘上长出两个垃圾目录 → 污染 reactor。

    铁律：**光凭契约里一个字符串，不足以在磁盘上造一个模块。**
    必须有独立证据：计划里有子任务往 `<mod>/` 下写代码（pom 本身不算——那是循环论证）。
    """
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["alarm-web/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "alarm-web", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "module", "artifacts": ["org.projectlombok:lombok"]},      # ← schema 占位符
        {"module": "artifacts", "artifacts": ["org.projectlombok:lombok"]},   # ← schema 占位符
    ]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    mods = {e["module"] for e in injected}
    assert mods == {"alarm-core", "alarm-web"}, f"无代码证据的模块名必须拒绝脚手架，实得 {mods}"
    assert not any(st.id.startswith("st-scaffold-module") for st in plan.subtasks)
    assert not any(st.id.startswith("st-scaffold-artifacts") for st in plan.subtasks)


def test_scaffold_accepts_module_present_in_baseline_reactor(tmp_path):
    """基线里**真实存在的模块目录**（棕地既有模块）→ 即便本轮无人写它的代码，也是真模块，允许补 pom。

    证据必须是**栈中立**的：目录存在（任何栈的模块都有目录），而不是去解析某一种构建清单。
    """
    (tmp_path / "pom.xml").write_text(
        "<project><artifactId>ruoyi</artifactId>"
        "<modules><module>ruoyi-common</module></modules></project>", encoding="utf-8")
    (tmp_path / "ruoyi-common" / "src").mkdir(parents=True)   # 棕地既有模块：目录真实存在
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["alarm-web/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "alarm-web", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "ruoyi-common", "artifacts": ["org.projectlombok:lombok"]},  # 基线真模块
    ]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    assert "ruoyi-common" in {e["module"] for e in injected}, "基线 reactor 成员是真模块，必须允许"


# ── R57-4：模块的【物理落点】必须由计划 scope 自证；契约模块名只是标签 ────────────

def test_scaffold_pom_lands_where_the_code_actually_lives(tmp_path):
    """★R57-4 P0（round57 头号杀手）★ 脚手架把 pom 建在了【契约模块名】处，而代码在别的目录。

    round57 实锤：契约模块名是 `alarm-core`，脚手架就建 `alarm-core/pom.xml`（根级），
    但计划里的代码全落在 `ruoyi-alarm/alarm-core/` 下 → 两套口径分叉：
      · st-13 验收 `mvn compile -pl ruoyi-alarm/alarm-interface -am` → reactor 里找不到该项目
      · st-21 验收 `test -f ruoyi-alarm/pom.xml` → 父 POM 根本没人建
    → 3 个子任务全灭 → 阶梯三保 build → **连坐放弃下游 69 个**（一条根因炸掉 79% 的计划）。

    铁律：**模块 = 物理路径**，由计划的真实 scope 自证；契约里的模块名只是个标签。
    """
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-alarm/alarm-web/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "alarm-web", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))
    poms = {f for e in injected
            for st in plan.subtasks if st.id == e["subtask_id"]
            for f in (list(st.scope.create_files) + list(st.scope.writable))}
    assert "ruoyi-alarm/alarm-core/pom.xml" in poms, f"pom 必须建在代码真实所在处，实得 {poms}"
    assert "ruoyi-alarm/alarm-web/pom.xml" in poms
    assert "alarm-core/pom.xml" not in poms, "绝不能按契约模块名字面建在根级（口径分叉的源头）"


def test_aggregator_parent_pom_is_scaffolded_first(tmp_path):
    """★R57-4b★ 子模块都在 `ruoyi-alarm/` 下 → 聚合父 POM 必须**确定性地先建出来**。

    round57 实锤：父 POM `ruoyi-alarm/pom.xml` 的创建权被分给了 st-1，而 st-1 又依赖
    st-13/21/39 → **依赖顺序死结** → 那三个子任务编译时父 POM 不存在 → 全灭。
    父聚合模块拓扑上必须先于所有子模块，且**不依赖任何子模块**。
    """
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-alarm/alarm-web/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "alarm-web", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))
    agg = next((st for st in plan.subtasks
                if "ruoyi-alarm/pom.xml" in (list(st.scope.create_files)
                                             + list(st.scope.writable))), None)
    assert agg is not None, "聚合父 POM 必须有确定性的创建者（否则子模块永远编译不了）"
    assert not agg.depends_on, "父聚合模块绝不能依赖任何子模块（那正是 round57 的依赖死结）"
    kids = [st for st in plan.subtasks
            if st.id.startswith("st-scaffold-") and st.id != agg.id]
    assert kids and all(agg.id in st.depends_on for st in kids), "子模块脚手架必须依赖父 POM 先建好"


# ── R57-7：parent GAV 必须与【真实的上级 pom】一致（发版前推演揪出，未进 E2E 就治） ──

def test_child_module_parent_gav_matches_the_aggregator_not_the_root(tmp_path):
    """★R57-7（推演揪出，round57 的 FATAL 原文）★ 子模块的 <parent> 必须指向**真实上级 pom**。

    Maven 的 <parent> 默认 relativePath 是 `../pom.xml`。模块住在 `ruoyi-alarm/alarm-core/` 时，
    `../pom.xml` = `ruoyi-alarm/pom.xml`（聚合父）。若模板把 parent GAV 写成**根工程**的坐标，
    二者对不上 → Maven 直接 FATAL（round57 实锤原文）：
        Non-resolvable parent POM for com.ruoyi:ruoyi-alarm-security:
        Could not find artifact com.ruoyi:ruoyi-alarm:pom ... 'parent.relativePath' points at wrong local POM

    R57-4b 注入聚合父之后，这条**必然**发生——不推演就发版，round58 会死在一模一样的错误上。
    """
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
        "<packaging>pom</packaging></project>", encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-alarm/alarm-web/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "alarm-web", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))

    child = next(st for st in plan.subtasks if st.id == "st-scaffold-alarm-core")
    assert "<artifactId>ruoyi-alarm</artifactId>" in child.description, (
        "子模块的 parent 必须是**聚合父** ruoyi-alarm，不是根工程 ruoyi"
        "（写根工程 → relativePath ../pom.xml 指到聚合父 → GAV 对不上 → FATAL）")

    agg = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm")
    assert "<packaging>pom</packaging>" in agg.description, "聚合父必须是 packaging=pom"
    assert "<artifactId>ruoyi</artifactId>" in agg.description, "聚合父自己的 parent = 根工程"
    for m in ("<module>alarm-core</module>", "<module>alarm-web</module>"):
        assert m in agg.description, f"聚合父必须登记子模块 {m}"
    assert injected


def test_aggregator_pom_injected_even_when_all_submodule_poms_are_claimed(tmp_path):
    """★R60-1（round60 死因）★ 子模块 pom 全被认领 → entries 空 → 聚合父注入被跳过 → FATAL。

    R58-3 太成功了：8 个子模块的 pom 都被写代码的子任务认领、拿到确定性模板 →
    `unclaimed_contract_deps` 返回空 → `inject_build_scaffold_subtasks` 提前 return
    → **聚合父脚手架 `_inject_aggregator_scaffold` 根本没机会运行**。
    而聚合父 pom（纯 packaging=pom、无代码）**没有任何子任务会认领它** →
    `ruoyi-alarm/pom.xml` 没人建 → 所有子模块 parent `com.ruoyi:ruoyi-alarm:pom` 找不到 → 全员 FATAL。

    聚合父的存在性**与子模块 pom 有没有 owner 无关**：只要子模块同处一个非根聚合目录，
    那个聚合父 pom 就必须有确定性的创建者。
    """
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
        "<packaging>pom</packaging></project>", encoding="utf-8")

    def _st(sid, create):
        return SubTask(id=sid, description=f"task {sid}",
                       difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(create_files=create))

    # 每个子模块 pom 都被写代码的子任务认领（round60 实况）
    plan = TaskPlan(subtasks=[
        _st("st-1", ["ruoyi-alarm/alarm-core/pom.xml",
                     "ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", ["ruoyi-alarm/alarm-channel/pom.xml",
                     "ruoyi-alarm/alarm-channel/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": []},
        {"module": "alarm-channel", "artifacts": []}]}
    file_plan = [{"module": "alarm-core", "path": "ruoyi-alarm/alarm-core/src/main/java/A.java"},
                 {"module": "alarm-channel", "path": "ruoyi-alarm/alarm-channel/src/main/java/B.java"}]

    inject_build_scaffold_subtasks(plan, str(tmp_path), file_plan)

    agg = next((st for st in plan.subtasks
                if "ruoyi-alarm/pom.xml" in (list(st.scope.create_files)
                                             + list(st.scope.writable))), None)
    assert agg is not None, (
        "子模块 pom 全被认领时，聚合父 ruoyi-alarm/pom.xml 仍必须有确定性创建者"
        "（否则所有子模块的 parent POM 找不到 → 全员 FATAL）")
    assert not agg.depends_on, "聚合父绝不依赖任何子模块"
    # 子模块认领者应依赖聚合父先落地
    for sid in ("st-1", "st-2"):
        st = next(s for s in plan.subtasks if s.id == sid)
        assert agg.id in st.depends_on, f"{sid} 必须依赖聚合父 {agg.id} 先建好"


def test_aggregator_injected_even_when_someone_claimed_its_pom(tmp_path):
    """★R61-1（round61 死因）★ 写代码的子任务顺手认领了聚合父 pom → 绝不让位，收回写权。

    round61 实锤：某子任务认领了 `ruoyi-alarm/pom.xml`，R60-1 判"已有 owner → 不注入脚手架"。
    但认领者不保证拓扑最先/内容正确 → 子模块编译时父 POM 找不到 → `Non-resolvable parent POM`
    → 全员 FATAL（round57 原始死因复活）。聚合父必须由确定性脚手架**独占**其写权、拓扑最先。
    """
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version><packaging>pom</packaging></project>",
        encoding="utf-8")

    def _st(sid, create):
        return SubTask(id=sid, description="d", difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(create_files=create))

    # st-1 写 alarm-api 的代码，**并顺手认领了聚合父 ruoyi-alarm/pom.xml**
    plan = TaskPlan(subtasks=[
        _st("st-1", ["ruoyi-alarm/pom.xml",
                     "ruoyi-alarm/alarm-api/src/main/java/A.java"]),
        _st("st-2", ["ruoyi-alarm/alarm-engine/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-api", "artifacts": []},
        {"module": "alarm-engine", "artifacts": []}]}
    file_plan = [{"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/A.java"},
                 {"module": "alarm-engine", "path": "ruoyi-alarm/alarm-engine/src/main/java/B.java"}]
    inject_build_scaffold_subtasks(plan, str(tmp_path), file_plan)

    agg = next((st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm"), None)
    assert agg is not None, "聚合父必须由确定性脚手架建，绝不让位给认领者"
    assert not agg.depends_on, "聚合父不依赖任何子模块"
    # 写权已从 st-1 收回
    st1 = next(s for s in plan.subtasks if s.id == "st-1")
    st1_owns = list(st1.scope.create_files) + list(st1.scope.writable)
    assert "ruoyi-alarm/pom.xml" not in st1_owns, "聚合父 pom 写权必须从认领者手里收回（脚手架独占）"
    assert agg.id in st1.depends_on, "st-1（写 alarm-api 代码）必须依赖聚合父先落地"


def test_multiple_aggregator_dirs_each_get_a_parent(tmp_path):
    """★R61-1★ 模块分处**多个**聚合目录 → 每个聚合目录各注入一个父 POM（不再"歧义→一个不建"）。"""
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version><packaging>pom</packaging></project>",
        encoding="utf-8")

    def _st(sid, create):
        return SubTask(id=sid, description="d", difficulty=SubTaskDifficulty.MEDIUM,
                       scope=FileScope(create_files=create))

    plan = TaskPlan(subtasks=[
        _st("st-1", ["ruoyi-alarm/alarm-api/src/main/java/A.java"]),
        _st("st-2", ["ruoyi-biz/biz-core/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-api", "artifacts": []},
        {"module": "biz-core", "artifacts": []}]}
    file_plan = [{"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/A.java"},
                 {"module": "biz-core", "path": "ruoyi-biz/biz-core/src/main/java/B.java"}]
    inject_build_scaffold_subtasks(plan, str(tmp_path), file_plan)

    aggs = {st.id for st in plan.subtasks if st.id.startswith("st-scaffold-ruoyi-")}
    assert "st-scaffold-ruoyi-alarm" in aggs and "st-scaffold-ruoyi-biz" in aggs, (
        f"两个聚合目录都必须各注入一个父 POM，实得 {aggs}")


def test_multi_aggregator_module_scaffold_depends_on_its_own_parent(tmp_path):
    """★R61-2（对抗复核实锤）★ 多聚合场景：每个子模块脚手架必须依赖**它自己所在聚合目录**的父 POM。

    R61-1 曾用单个 `last_sid`（排序最后一个聚合）给**所有**子模块脚手架挂父依赖边 → `ruoyi-alarm`
    下的模块被错挂到 `ruoyi-biz` 的父上、且**漏掉真父** → 调度可能在 `ruoyi-alarm/pom.xml` 建好前
    就跑子模块 → `Non-resolvable parent POM` → round57 死因原样复活（且只在≥2 聚合时触发，单聚合
    的 RuoYi-E2E 测不到）。此测试在 entries 路径（模块有 artifacts → 生成 st-scaffold-<mod>）上锁死。
    """
    from swarm.brain.contract_utils import inject_build_scaffold_subtasks

    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version><packaging>pom</packaging></project>",
        encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-biz/biz-core/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "biz-core", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    alarm_mod = next(st for st in plan.subtasks if st.id == "st-scaffold-alarm-core")
    biz_mod = next(st for st in plan.subtasks if st.id == "st-scaffold-biz-core")
    assert "st-scaffold-ruoyi-alarm" in alarm_mod.depends_on, (
        "alarm-core 脚手架必须依赖它自己的聚合父 ruoyi-alarm 先落地")
    assert "st-scaffold-ruoyi-biz" not in alarm_mod.depends_on, (
        "alarm-core 绝不能错挂到 ruoyi-biz 的父上（R61-1 单 last_sid 的病）")
    assert "st-scaffold-ruoyi-biz" in biz_mod.depends_on, (
        "biz-core 脚手架必须依赖它自己的聚合父 ruoyi-biz 先落地")
    assert "st-scaffold-ruoyi-alarm" not in biz_mod.depends_on, (
        "biz-core 绝不能错挂到 ruoyi-alarm 的父上")


# ── Task#4（round62 真断治本）：聚合器 <modules> 必须登记**全部收码物理子模块** ──

def _root_pom(tmp_path):
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version>"
        "<packaging>pom</packaging></project>", encoding="utf-8")


def test_task4_orphan_physical_module_registered_in_aggregator_modules(tmp_path):
    """★round62 真断★ 一个收了码、但**契约里没有它**（非干净契约模块）的物理子模块，
    必须仍被登记进聚合父 <modules>——否则 Maven 不下钻 = **静默丢模块**（无报错）。"""
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),   # 干净契约模块
        _st("st-2", create=["ruoyi-alarm/alarm-extra/src/main/java/B.java"]),  # 孤儿：契约里没有
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    agg = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm")
    assert "<module>alarm-core</module>" in agg.description, "干净契约模块须登记"
    assert "<module>alarm-extra</module>" in agg.description, (
        "★治本核心★ 收码但非契约模块的 alarm-extra 也**必须**进聚合父 <modules>，"
        "否则 mvn 不下钻 → round62 静默丢模块")


def test_task4_orphan_gets_deterministic_pom_scaffold_with_aggregator_parent(tmp_path):
    """孤儿模块被登记后，Maven 会下钻找它的 pom → 必须有确定性 pom owner（parent=聚合父），
    否则 `child module ... does not exist` = 派 worker 去失败。"""
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-alarm/alarm-extra/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]}]}
    injected = inject_build_scaffold_subtasks(plan, str(tmp_path))

    orphan = next((st for st in plan.subtasks
                   if st.id == "st-scaffold-ruoyi-alarm-alarm-extra"), None)
    assert orphan is not None, "孤儿模块必须有确定性 pom 脚手架"
    assert "ruoyi-alarm/alarm-extra/pom.xml" in list(orphan.scope.create_files)
    assert "<artifactId>ruoyi-alarm</artifactId>" in orphan.description, (
        "孤儿 pom 的 parent 必须是**聚合父** ruoyi-alarm（relativePath ../pom.xml），"
        "绝不能写根工程 ruoyi → GAV 对不上 → round57 FATAL")
    assert "<packaging>jar</packaging>" in orphan.description
    assert "st-scaffold-ruoyi-alarm" in orphan.depends_on, "孤儿脚手架依赖聚合父先落地"
    assert any(e.get("orphan") for e in injected), "injected 应记录孤儿条目"


def test_task4_orphan_scaffolds_are_collision_safe_across_aggregators(tmp_path):
    """★Task#4 预判：slug 撞车★ 不同聚合父下的**同名叶子**孤儿模块，脚手架 id 必须互不撞车
    （用完整物理路径，不用叶子名），且各自 parent 指向**自己的**聚合父。"""
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/common/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-biz/common/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": []}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    ids = {st.id for st in plan.subtasks}
    assert "st-scaffold-ruoyi-alarm-common" in ids
    assert "st-scaffold-ruoyi-biz-common" in ids, "同名叶子 common 绝不能 slug 撞车成一个"
    alarm_orphan = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm-common")
    biz_orphan = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-biz-common")
    assert "st-scaffold-ruoyi-alarm" in alarm_orphan.depends_on
    assert "st-scaffold-ruoyi-biz" in biz_orphan.depends_on
    assert "st-scaffold-ruoyi-biz" not in alarm_orphan.depends_on, "绝不错挂到别的聚合父"


def test_task4_baseline_orphan_pom_respected_not_clobbered(tmp_path):
    """孤儿目录在**基线已有 pom** → 只补登记进 <modules>，绝不建脚手架覆盖既有（clobber 更致命）。"""
    _root_pom(tmp_path)
    (tmp_path / "ruoyi-alarm/alarm-extra").mkdir(parents=True)
    (tmp_path / "ruoyi-alarm/alarm-extra/pom.xml").write_text(
        '<?xml version="1.0"?><project><artifactId>alarm-extra</artifactId></project>',
        encoding="utf-8")
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", writable=["ruoyi-alarm/alarm-extra/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": []}]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    agg = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm")
    assert "<module>alarm-extra</module>" in agg.description, "既有孤儿模块仍须登记进 <modules>"
    assert not any(st.id == "st-scaffold-ruoyi-alarm-alarm-extra" for st in plan.subtasks), (
        "基线已有 pom → 绝不建脚手架 clobber 既有内容")


def test_task4_orphan_pom_write_reclaimed_from_code_writer(tmp_path):
    """写代码子任务顺手认领了孤儿 pom → 脚手架**收回**其写权（同 R57-6：构建文件是机械产物，
    多写者 rebase 不收敛 + 手写 parent 坐标风险），认领者转为依赖脚手架先落地。"""
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-alarm/alarm-extra/pom.xml",
                            "ruoyi-alarm/alarm-extra/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": []}]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    st2 = next(st for st in plan.subtasks if st.id == "st-2")
    st2_files = list(st2.scope.create_files) + list(st2.scope.writable)
    assert "ruoyi-alarm/alarm-extra/pom.xml" not in st2_files, (
        "孤儿 pom 写权必须从写代码子任务手里收回，交确定性脚手架独占")
    assert "ruoyi-alarm/alarm-extra/src/main/java/B.java" in st2_files, "代码文件写权保留"
    assert "st-scaffold-ruoyi-alarm-alarm-extra" in st2.depends_on, "认领者转为依赖脚手架先落地"


def test_task4_plan_structure_stays_valid_with_orphans(tmp_path):
    """治本后计划仍满足结构不变量（全员入组、依赖可解）。"""
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/alarm-core/src/main/java/A.java"]),
        _st("st-2", create=["ruoyi-alarm/alarm-extra/src/main/java/B.java"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-core", "artifacts": []}]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))
    validate_plan_structure(plan)   # 不抛 = 结构不变量守约


def test_task4_nested_aggregator_parent_gav_points_to_immediate_parent(tmp_path):
    """★双复核 HIGH★ 多级嵌套聚合器：中间层聚合器的 <parent> 必须指向**直接上级聚合目录**，
    绝不能一律写根工程——否则 relativePath ../pom.xml 指到的上级 GAV 对不上 → round57 FATAL。
    且每一层聚合器都必须被注入（漏中间层 = 子模块 parent 链断）。"""
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/inner/leaf/src/main/java/A.java"]),
    ], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": []}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    ids = {st.id for st in plan.subtasks}
    top = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm")
    mid = next(st for st in plan.subtasks if st.id == "st-scaffold-ruoyi-alarm-inner")
    assert "st-scaffold-ruoyi-alarm-inner-leaf" in ids, "叶子模块须有脚手架"
    # 顶层聚合器 parent = 根工程 ruoyi；中间层聚合器 parent = 直接上级 ruoyi-alarm
    assert "<artifactId>ruoyi</artifactId>" in top.description, "顶层聚合器 parent = 根工程"
    assert "<artifactId>ruoyi-alarm</artifactId>" in mid.description, (
        "★核心★ 中间层聚合器 ruoyi-alarm/inner 的 parent 必须是直接上级 ruoyi-alarm，"
        "绝不能是根工程 ruoyi（../pom.xml GAV 对不上 → round57 FATAL）")
    assert "<module>inner</module>" in top.description, "顶层聚合器须登记中间层 inner"
    assert "<module>leaf</module>" in mid.description, "中间层聚合器须登记叶子 leaf"
    # 中间层聚合器依赖顶层聚合器先落地；顶层不依赖任何人
    assert "st-scaffold-ruoyi-alarm" in mid.depends_on, "中间层依赖顶层聚合器先落地"
    assert not top.depends_on, "顶层聚合器绝不依赖任何子级"
    validate_plan_structure(plan)


def test_task4_self_reflexive_aggregator_with_own_code_is_flagged_loudly(tmp_path, caplog):
    """★双复核 HIGH（结构冲突）★ 一个目录既有直接代码又是聚合父 → Maven 无法两全，
    绝不能静默产出丢代码的 packaging=pom → 必须 LOUD 告警交 plan-quality 复核。"""
    import logging
    _root_pom(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-1", create=["ruoyi-alarm/core/src/main/java/Own.java"]),      # core 自身有码
        _st("st-2", create=["ruoyi-alarm/core/sub/src/main/java/Sub.java"]),  # 且是 sub 的聚合父
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": []}
    with caplog.at_level(logging.WARNING):
        inject_build_scaffold_subtasks(plan, str(tmp_path))
    assert any("结构冲突" in r.message and "ruoyi-alarm/core" in str(r.args)
               for r in caplog.records), (
        "既是收码模块又是聚合父的目录必须被 LOUD 标记为计划质量缺陷，绝不静默丢代码")
