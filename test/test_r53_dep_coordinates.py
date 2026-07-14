"""R53 治本锁：依赖坐标解析（R53-1）+ 幻影依赖剪除（R53-2）。

round53 实锤死因链（本文件逐环上锁）：
  契约引入基线外第三方 → 模板只能"伪造(禁)/省略" → 省略成空壳 pom，验收标准却仍要求
  "声明契约全部 artifacts" → **自相矛盾逼 worker 手写坐标** → worker 臆造出无 <version>
  的幻影坐标（com.ruoyi:alarm-interface / com.alarm.platform:alarm-interface 同物两 group）
  → `'dependencies.dependency.version' … is missing` 是 pom **解析期**错 → Maven 连 reactor
  都读不出 → 全体 worker 构建闸 BLOCKED → 编译验证失效 → 8/80 判死。

所有网络调用在测试里被 monkeypatch 掉：解析器必须**离线可降级**（退回省略旧行为），
且**绝不因网络不可用而伪造坐标**。
"""
from __future__ import annotations

import pytest

from swarm.brain import maven_registry as mr
from swarm.brain.contract_utils import (
    _deterministic_pom_template,
    inject_build_scaffold_subtasks,
    resolve_scaffold_artifacts,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskIntent, TaskPlan
from swarm.worker.l1_pipeline import _prune_dep_blocks, _reactor_artifacts

ROOT_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project>
    <groupId>com.ruoyi</groupId>
    <artifactId>ruoyi</artifactId>
    <version>4.8.3</version>
    <packaging>pom</packaging>
    <properties><spring-boot.version>4.0.6</spring-boot.version></properties>
    <modules>
        <module>ruoyi-common</module>
        <module>alarm-api</module>
    </modules>
    <dependencyManagement>
        <dependencies>
            <dependency>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-dependencies</artifactId>
                <version>${spring-boot.version}</version>
                <type>pom</type>
                <scope>import</scope>
            </dependency>
            <dependency>
                <groupId>com.alibaba</groupId>
                <artifactId>druid-spring-boot-4-starter</artifactId>
                <version>1.2.23</version>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>
"""


@pytest.fixture()
def project(tmp_path, monkeypatch):
    (tmp_path / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    # BOM 受管集：模拟 spring-boot-dependencies 真实管辖（lombok 管、hutool 不管）
    monkeypatch.setattr(mr, "bom_managed_artifacts",
                        lambda g, a, v: {"lombok": "org.projectlombok",
                                         "spring-boot-starter-web": "org.springframework.boot",
                                         "mysql-connector-j": "com.mysql"}
                        if a == "spring-boot-dependencies" else {})
    mr._http_cache.clear()
    return tmp_path


def test_bom_managed_artifact_gets_no_version(project):
    """受管依赖（BOM 传递闭包内）→ 不写 <version>：写死会覆盖工程统一版本。"""
    kept, dropped = mr.resolve_artifacts(str(project), ["lombok"])
    assert dropped == []
    assert len(kept) == 1 and kept[0].artifact == "lombok"
    assert kept[0].version is None, "BOM 管得到 → 按 Maven 惯例不写版本"


def test_unmanaged_third_party_must_carry_explicit_version(project, monkeypatch):
    """R52-2/R53 核心不变量：父级管不到的第三方 **必须**带显式版本，否则整树读不出。"""
    monkeypatch.setattr(mr, "registry_group_for",
                        lambda a: "cn.hutool" if a == "hutool-all" else None)
    monkeypatch.setattr(mr, "registry_latest_version",
                        lambda g, a: "5.8.47" if (g, a) == ("cn.hutool", "hutool-all") else None)
    kept, dropped = mr.resolve_artifacts(str(project), ["hutool-all"])
    assert dropped == []
    assert kept[0].group == "cn.hutool" and kept[0].version == "5.8.47"

    tpl = _deterministic_pom_template("alarm-x", ["hutool-all"], str(project), resolved=kept)
    assert "<groupId>cn.hutool</groupId>" in tpl
    assert "<version>5.8.47</version>" in tpl


def test_phantom_artifact_is_dropped_not_forged(project, monkeypatch):
    """幻影 artifact（仓库查无、非 reactor 模块）→ 如实丢弃；绝不回退工程 groupId（R47-2 铁律）。"""
    monkeypatch.setattr(mr, "registry_group_for", lambda a: None)
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: None)
    kept, dropped = mr.resolve_artifacts(str(project), ["alarm-interface"])
    assert kept == [] and dropped == ["alarm-interface"]

    tpl = _deterministic_pom_template("alarm-x", ["alarm-interface"], str(project), resolved=kept)
    assert "alarm-interface" not in tpl, "幻影坐标绝不进权威模板"
    assert "com.ruoyi</groupId>\n            <artifactId>alarm-interface" not in tpl


def test_reactor_sibling_module_gets_project_version(project, monkeypatch):
    """reactor 兄弟模块（父级漏管）→ ${project.version}，不写死、不去仓库找（仓库本就没有）。"""
    monkeypatch.setattr(mr, "registry_group_for", lambda a: None)
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: None)
    kept, dropped = mr.resolve_artifacts(str(project), ["alarm-api"])
    assert dropped == []
    assert kept[0].group == "com.ruoyi" and kept[0].version == "${project.version}"


def test_offline_degrades_to_omission_never_forgery(project, monkeypatch):
    """离线（仓库查不通）→ 退回旧行为：省略。绝不因为查不到就猜坐标。"""
    monkeypatch.setattr(mr, "_http_get", lambda url: None)
    kept, dropped = mr.resolve_artifacts(str(project), ["hutool-all", "fastjson2"])
    assert kept == []
    assert sorted(dropped) == ["fastjson2", "hutool-all"]


def test_scaffold_contract_and_acceptance_are_same_source_as_template(project, monkeypatch):
    """★R53-1 头号锁★ 模板 / 契约 / 验收标准**同源**——解析不到的依赖三处一并剔除。

    旧实现：模板省略 hutool，验收却写"声明契约 **全部** artifacts" → worker 被逼手写坐标。
    round52 的 replan LLM 明确抱怨过这条矛盾（"权威模板仅声明 5 个依赖…与验收标准矛盾"），
    它是对的——是系统在逼它造假。
    """
    monkeypatch.setattr(mr, "registry_group_for",
                        lambda a: "cn.hutool" if a == "hutool-all" else None)
    monkeypatch.setattr(mr, "registry_latest_version",
                        lambda g, a: "5.8.47" if a == "hutool-all" else None)
    plan = TaskPlan(
        subtasks=[SubTask(
            id="st-1", description="写代码", intent=TaskIntent.CREATE,
            difficulty=SubTaskDifficulty.MEDIUM,
            scope=FileScope(create_files=["alarm-x/src/main/java/A.java"]),
        )],
        parallel_groups=[["st-1"]],
    )
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-x", "artifacts": ["hutool-all", "alarm-interface"]}]}

    injected = inject_build_scaffold_subtasks(plan, str(project))
    assert injected, "落空模块必须注入脚手架"
    scaffold = next(st for st in plan.subtasks if st.id == "st-scaffold-alarm-x")

    arts = scaffold.contract["dependencies"][0]["artifacts"]
    assert any("hutool-all" in a for a in arts), "可解析依赖保留在契约里"
    assert not any("alarm-interface" in a for a in arts), (
        "★幻影依赖必须同时从契约剔除——否则验收标准仍要求声明它，worker 只能编坐标")
    assert "alarm-interface" not in scaffold.description, "幻影坐标不得出现在权威模板/描述里"
    assert "5.8.47" in scaffold.description, "可解析依赖须带显式版本进模板"


# ── R53-2：worker 侧幻影依赖确定性剪除 ──────────────────────────────────────
POM_WITH_PHANTOM = """<project>
    <artifactId>alarm-engine</artifactId>
    <dependencies>
        <dependency>
            <groupId>cn.hutool</groupId>
            <artifactId>hutool-all</artifactId>
            <version>5.8.47</version>
        </dependency>
        <dependency>
            <groupId>com.ruoyi</groupId>
            <artifactId>alarm-interface</artifactId>
        </dependency>
    </dependencies>
</project>
"""


def test_prune_removes_versionless_phantom_only():
    out = _prune_dep_blocks(POM_WITH_PHANTOM, "com.ruoyi", "alarm-interface")
    assert out is not None
    assert "alarm-interface" not in out, "幻影依赖块必须被剪除（否则整 reactor 读不出）"
    assert "hutool-all" in out and "5.8.47" in out, "带版本的正常依赖不得被误伤"
    assert out.count("<dependency>") == 1


def test_prune_keeps_block_that_has_version():
    """有 <version> 的依赖顶多解析失败（可归因）→ 不在剪除范围（fail-safe，绝不扩大打击面）。"""
    text = POM_WITH_PHANTOM.replace(
        "            <artifactId>alarm-interface</artifactId>\n",
        "            <artifactId>alarm-interface</artifactId>\n            <version>1.0</version>\n")
    assert _prune_dep_blocks(text, "com.ruoyi", "alarm-interface") is None


def test_prune_ignores_groupid_mismatch_and_exclusions():
    assert _prune_dep_blocks(POM_WITH_PHANTOM, "com.other", "alarm-interface") is None, \
        "groupId 明确不匹配 → 不剪"
    excl = """<project><dependencies>
        <dependency>
            <groupId>org.x</groupId>
            <artifactId>lib</artifactId>
            <version>1.0</version>
            <exclusions>
                <exclusion><groupId>com.ruoyi</groupId><artifactId>ghost</artifactId></exclusion>
            </exclusions>
        </dependency>
    </dependencies></project>"""
    assert _prune_dep_blocks(excl, "com.ruoyi", "ghost") is None, \
        "exclusions 内撞名不得触发剪除（外层依赖有版本且合法）"


def test_reactor_artifacts_reads_modules_and_root(tmp_path):
    (tmp_path / "pom.xml").write_text(ROOT_POM, encoding="utf-8")
    import swarm.worker.l1_pipeline as lp
    mods = _reactor_artifacts.__wrapped__(str(tmp_path)) if hasattr(
        _reactor_artifacts, "__wrapped__") else None
    # _read_project_file 走沙箱通道 → 直接注入本地读取
    lp._read_project_file = lambda p, rel, timeout=20: (  # type: ignore[assignment]
        (tmp_path / rel).read_text("utf-8") if (tmp_path / rel).is_file() else None)
    mods = lp._reactor_artifacts(str(tmp_path))
    assert {"ruoyi-common", "alarm-api", "ruoyi"} <= mods
    assert "alarm-interface" not in mods, "不存在的模块绝不能被当成 reactor 成员（否则幻影逃逸）"
