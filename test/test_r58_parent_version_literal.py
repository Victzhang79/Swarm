"""R58-2 治本锁：pom 的 <parent><version> **必须是字面量**（Maven 硬规则）。

★round58 死因（实锤原文）★
    [FATAL] Non-resolvable parent POM for com.ruoyi:alarm-api:${ruoyi.version}:
            Could not find artifact com.ruoyi:ruoyi:pom:${ruoyi.version}

LLM 把子模块的 parent 版本写成 `${ruoyi.version}` —— 属性引用。Maven **永远**解析不了它：
属性定义在**父 pom 里**，而 Maven 此刻还没加载父 pom（先有鸡还是先有蛋）。
后果是 **pom 解析期**崩塌 → 整棵 reactor 读不出 → 全员构建闸 BLOCKED（round51-53 的同一死法）。

**为什么确定性模板没救到它**：round58 一个脚手架都没注入——计划里每个模块的 pom 都已被某个
写代码的子任务"认领"，规则5 判"有 owner 就不建脚手架"。于是这些 pom **全是 LLM 手写的**。
**有 owner ≠ 有确定性模板** —— R45-2（"pom 是纯机械产物，别让小模型编"）在这条路径上完全落空。

本锁是**确定性兜底**：不论 pom 由谁写出来，进 Maven 之前把 parent 版本还原成字面量。
"""
from __future__ import annotations

from swarm.worker.l1_pipeline import _fix_parent_version_literal

ROOT = """<project>
    <groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>4.8.3</version>
    <packaging>pom</packaging>
    <properties><ruoyi.version>4.8.3</ruoyi.version></properties>
</project>
"""


def test_property_ref_parent_version_is_replaced_with_literal():
    """`${ruoyi.version}` → `4.8.3`（取自根 pom 的字面 version）。"""
    pom = """<project>
    <parent>
        <groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>
        <version>${ruoyi.version}</version>
    </parent>
    <artifactId>alarm-api</artifactId>
</project>
"""
    out = _fix_parent_version_literal(pom, root_text=ROOT)
    assert out is not None, "属性引用的 parent 版本必须被还原"
    assert "<version>4.8.3</version>" in out
    assert "${ruoyi.version}" not in out


def test_literal_parent_version_is_untouched():
    """已是字面量 → 一个字符都不动（绝不无谓改写）。"""
    pom = ("<project><parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
           "<version>4.8.3</version></parent><artifactId>x</artifactId></project>")
    assert _fix_parent_version_literal(pom, root_text=ROOT) is None


def test_only_parent_block_version_is_touched():
    """只动 <parent> 里的 version——依赖块里的 ${...} 版本是**合法的**，绝不能误改。"""
    pom = """<project>
    <parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>
        <version>${ruoyi.version}</version></parent>
    <artifactId>alarm-api</artifactId>
    <dependencies>
        <dependency><groupId>com.ruoyi</groupId><artifactId>ruoyi-common</artifactId>
            <version>${project.version}</version></dependency>
    </dependencies>
</project>
"""
    out = _fix_parent_version_literal(pom, root_text=ROOT)
    assert out is not None
    assert out.count("4.8.3") == 1, "只还原 parent 的版本"
    assert "${project.version}" in out, "依赖块里的属性引用是合法的，绝不能动"


def test_no_root_version_fails_open():
    """根 pom 拿不到字面 version（继承 GAV 等）→ 不动（fail-open：绝不猜版本）。"""
    pom = ("<project><parent><groupId>g</groupId><artifactId>a</artifactId>"
           "<version>${x.version}</version></parent><artifactId>m</artifactId></project>")
    assert _fix_parent_version_literal(pom, root_text="<project><artifactId>a</artifactId></project>") is None


# ── R58-3（结构性）：确定性 pom 模板必须触达【每一个】模块 pom，不管谁认领了它 ──

def test_authoritative_template_reaches_pom_owner_even_without_scaffold(tmp_path):
    """★R58-3（round58 结构性死因）★ 计划里 pom 已被写代码的子任务认领 → 旧规则不建脚手架
    → 那个 pom **完全没经过确定性模板**、由小模型手写 → 写出 `${ruoyi.version}` 的 parent → FATAL。

    **有 owner ≠ 有模板。** R45-2 的全部意义（"pom 是纯机械产物，别让小模型编"）在这条路径上落空。
    治：认领者也必须拿到**确定性权威模板**（嵌进它的 description，抄而不是编）。
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
                       scope=FileScope(writable=[], create_files=create))

    plan = TaskPlan(subtasks=[
        # 写代码的子任务顺手认领了 pom（round58 真实形态）→ 旧规则：不建脚手架
        _st("st-1", ["alarm-api/pom.xml", "alarm-api/src/main/java/A.java"]),
        _st("st-2", ["alarm-core/src/main/java/B.java", "alarm-core/pom.xml"]),
    ], parallel_groups=[["st-1", "st-2"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-api", "artifacts": ["org.projectlombok:lombok"]},
        {"module": "alarm-core", "artifacts": ["org.projectlombok:lombok"]},
    ]}
    inject_build_scaffold_subtasks(plan, str(tmp_path))

    for sid in ("st-1", "st-2"):
        st = next(s for s in plan.subtasks if s.id == sid)
        assert "权威 pom 模板" in st.description, (
            f"{sid} 认领了 pom 却没拿到确定性模板 → 小模型会手写出 ${{ruoyi.version}} 的 parent → FATAL")
        assert "<version>4.8.3</version>" in st.description, "parent 版本必须是**字面量**"


# ── R58-1：逻辑模块 → 物理目录，权威证据是 file_plan（不是名字匹配） ──────────

def test_logical_module_resolves_to_physical_dir_via_file_plan(tmp_path):
    """★R58-1（round58 实锤）★ 契约声明逻辑模块 `alarm-admin`，但它的代码落在基线既有模块
    `ruoyi-admin/` 里（**这是对的**——admin 功能加进现有模块，不该新建）。

    名字匹配必然失败（磁盘上没有 alarm-admin 目录）→ 8 条依赖契约**落空**、没人声明进
    `ruoyi-admin/pom.xml` → 编译期缺依赖。

    **权威证据不是名字，是 file_plan**：TECH_DESIGN 本就产出了【模块 → 文件】的归属
    （"模块 alarm-admin → 35 文件"），拿它求公共物理前缀即可，零猜测。
    """
    from swarm.brain.contract_utils import _module_physical_dirs
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    (tmp_path / "ruoyi-admin").mkdir()
    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d", difficulty=SubTaskDifficulty.MEDIUM,
                scope=FileScope(create_files=[
                    "ruoyi-admin/src/main/java/com/ruoyi/web/AlarmController.java"])),
    ], parallel_groups=[["st-1"]])
    plan.shared_contract = {"dependencies": [
        {"module": "alarm-admin", "artifacts": ["org.projectlombok:lombok"]}]}

    # 名字匹配：找不到（alarm-admin 不是任何路径段）
    assert _module_physical_dirs(plan, str(tmp_path)).get("alarm-admin") is None

    # file_plan 权威归属：模块 alarm-admin 的文件都在 ruoyi-admin/ 下 → 落点 = ruoyi-admin
    file_plan = [{"module": "alarm-admin",
                  "path": "ruoyi-admin/src/main/java/com/ruoyi/web/AlarmController.java"},
                 {"module": "alarm-admin",
                  "path": "ruoyi-admin/src/main/resources/templates/alarm.html"}]
    dirs = _module_physical_dirs(plan, str(tmp_path), file_plan=file_plan)
    assert dirs.get("alarm-admin") == "ruoyi-admin", (
        f"file_plan 是权威的【模块→文件】归属，必须据它定落点，实得 {dirs}")
