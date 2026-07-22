"""round67c 开箱验 plan 三路交叉定案治本用例（红灯先行）。

R67C-T1：create 撞 base 同名异路径的重复实体 → 归属闸盲区（编译绿、启动红）。
  round67c 实锤（task=626e35ae）：
  · st-4-1 create `ruoyi-system/.../system/domain/SysUser.java`，base canonical 在
    `ruoyi-common/.../common/core/domain/entity/SysUser.java`。`typeAliasesPackage:
    com.ruoyi.**.domain` 递归扫两处 → MyBatis TypeAliasRegistry 抛 'SysUser' already
    mapped → SqlSessionFactory 初始化崩 → Spring 上下文起不来。
  · st-51 create `ruoyi-admin/.../web/controller/tool/GenController.java`，base 已有
    `ruoyi-generator/.../generator/controller/GenController.java` → @Controller bean 名
    默认 simple name → 两份并存启动即 ConflictingBeanDefinitionException。
  归属闸 G1 ③(#110 同 FQN)/③b(R67-T1b 跨子任务同 basename)只查【跨子任务 create】、
  规则0 R67-T8 只查【create 撞同路径】，均漏【create 撞 base 异路径同 simple name】。
  治本（防御纵深，栈中立经 classpath_fqn_key，非 JVM 天然豁免）：
  · contract_utils 规则0 R67C-T1 自愈：create 撞 base 同名异路径、且 base canonical 已被
    他人 modify 佐证=确认同一实体重复 → 归位为 modify canonical（剔重复 create）；
  · plan_validator G1 ③f REJECT 兜底：残余无佐证 shadow → fail-closed 打回带双路径反馈。
"""
from __future__ import annotations

import subprocess

from swarm.brain.contract_utils import normalize_plan_scopes
from swarm.brain.plan_validator import validate_module_coherence
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_CANON = "ruoyi-common/src/main/java/com/ruoyi/common/core/domain/entity/SysUser.java"
_SHADOW = "ruoyi-system/src/main/java/com/ruoyi/system/domain/SysUser.java"
_GEN_BASE = "ruoyi-generator/src/main/java/com/ruoyi/generator/controller/GenController.java"
_GEN_SHADOW = "ruoyi-admin/src/main/java/com/ruoyi/web/controller/tool/GenController.java"


def _git_baseline(tmp_path, files):
    """在 tmp_path 建 git 仓并提交给定 base 文件，返回 (project_path, base_sha)。"""
    import os
    for rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("// base\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "base"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, env=env,
                       capture_output=True)
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    return str(tmp_path), sha


def _st(sid, *, create=None, writable=None, readable=None, depends=None, lang="java"):
    return SubTask(
        id=sid, description="d",
        scope=FileScope(writable=writable or [], create_files=create or [],
                        readable=readable or []),
        harness=TaskHarness(language=lang), depends_on=depends or [],
    )


# ── R67C-T1 G1 ③f REJECT（create 撞 base 同名异路径一律 fail-closed，不自愈）──
# ★不做规则0 自愈★：ecc 复核 HIGH 实锤——全局纯 basename 佐证会误把合法通用名新类(Config/Result)
# 归位改无关 base 文件=静默腐化，比治前显式异常更糟。改纯 ③f REJECT（LLM 可改名/改 modify）。
def test_t1_sysuser_shadow_rejected(tmp_path):
    """st-4-1 型：create system.domain.SysUser 撞 base common.entity.SysUser → G1 ③f 打回。"""
    proj, base = _git_baseline(tmp_path, [_CANON])
    plan = TaskPlan(subtasks=[
        _st("st-4-1", create=[_SHADOW]),
        _st("st-4-3", writable=[_CANON]),      # 他人 modify base 也不触发自愈（已砍）
    ])
    normalize_plan_scopes(plan, project_path=proj, base_ref=base)
    assert _SHADOW in (plan.subtasks[0].scope.create_files or []), "不再自愈：create 保留交 ③f"
    r = validate_module_coherence(plan, project_path=proj, base_ref=base)
    assert not r.valid and any("sysuser" in i.lower() for i in r.issues), r.issues


def test_t1_uncorroborated_shadow_rejected(tmp_path):
    """st-51 型：create 撞 base 同名异路径 → G1 ③f fail-closed 打回。"""
    proj, base = _git_baseline(tmp_path, [_GEN_BASE])
    plan = TaskPlan(subtasks=[_st("st-51", create=[_GEN_SHADOW])])
    normalize_plan_scopes(plan, project_path=proj, base_ref=base)
    assert _GEN_SHADOW in (plan.subtasks[0].scope.create_files or [])
    r = validate_module_coherence(plan, project_path=proj, base_ref=base)
    assert not r.valid and any("GenController" in i for i in r.issues), r.issues


# ── 误伤护栏 ───────────────────────────────────────────────────────────────
def test_t1_legit_new_class_untouched(tmp_path):
    """真新类（basename 不在 base）→ 不动、不打回。"""
    proj, base = _git_baseline(tmp_path, [_CANON])
    new = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmRule.java"
    plan = TaskPlan(subtasks=[_st("st-a", create=[new])])
    normalize_plan_scopes(plan, project_path=proj, base_ref=base)
    r = validate_module_coherence(plan, project_path=proj, base_ref=base)
    assert new in (plan.subtasks[0].scope.create_files or [])
    assert r.valid, r.issues


def test_t1_non_jvm_resource_exempt(tmp_path):
    """非 JVM 类路径源码（资源/模板同名）→ classpath_fqn_key None，天然豁免。"""
    base_html = "ruoyi-admin/src/main/resources/templates/system/user.html"
    shadow_html = "ruoyi-admin/src/main/resources/templates/alarm/user.html"
    proj, base = _git_baseline(tmp_path, [base_html])
    plan = TaskPlan(subtasks=[_st("st-a", create=[shadow_html], lang="java")])
    normalize_plan_scopes(plan, project_path=proj, base_ref=base)
    r = validate_module_coherence(plan, project_path=proj, base_ref=base)
    assert shadow_html in (plan.subtasks[0].scope.create_files or [])
    assert r.valid, r.issues


def test_t1_same_path_is_t8_not_t1(tmp_path):
    """create 撞 base 同路径 → R67-T8 降级 modify（不是 T1 shadow）。"""
    proj, base = _git_baseline(tmp_path, [_CANON])
    plan = TaskPlan(subtasks=[_st("st-a", create=[_CANON])])
    normalize_plan_scopes(plan, project_path=proj, base_ref=base)
    s = plan.subtasks[0].scope
    assert _CANON not in (s.create_files or []) and _CANON in (s.writable or [])


def test_t1_no_base_ref_noop(tmp_path):
    """非 git / 无 base → 规则0 跳过、G1 ③f 无 base 树 → 不误伤（greenfield）。"""
    plan = TaskPlan(subtasks=[_st("st-4-1", create=[_SHADOW])])
    normalize_plan_scopes(plan, project_path=None, base_ref=None)
    r = validate_module_coherence(plan, project_path=None, base_ref=None)
    assert _SHADOW in (plan.subtasks[0].scope.create_files or [])
    assert r.valid, r.issues


# ── R67C-T2：plan 新增模块须认作 reactor 兄弟，不被 R53-1 误剔 ─────────────────
_BASE_ROOT_POM = (
    "<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
    "<version>1.0</version><packaging>pom</packaging><modules>"
    "<module>ruoyi-admin</module><module>ruoyi-framework</module>"
    "<module>ruoyi-system</module><module>ruoyi-common</module></modules></project>"
)


def _base_proj(tmp_path):
    (tmp_path / "pom.xml").write_text(_BASE_ROOT_POM, encoding="utf-8")
    return str(tmp_path)


def test_t2_plan_new_module_kept_as_reactor_sibling(tmp_path):
    """干净 base（无 ruoyi-alarm）+ 传入 plan 新模块 → 解析为 ${project.version} 兄弟不丢。"""
    from swarm.brain.maven_registry import resolve_artifacts
    proj = _base_proj(tmp_path)
    kept, dropped = resolve_artifacts(
        proj, ["ruoyi-alarm", "ruoyi-framework"],
        extra_module_artifacts={"ruoyi-alarm", "ruoyi-alarm-interface"})
    arts = {r.artifact: r.version for r in kept}
    assert "ruoyi-alarm" not in dropped, dropped
    assert arts.get("ruoyi-alarm") == "${project.version}", arts


def test_t2_plan_module_artifacts_collects_from_contract_and_scope():
    """_plan_module_artifacts 从契约 dependencies + 子任务物理根收集 plan 自有模块。"""
    from swarm.brain.contract_utils import _plan_module_artifacts
    plan = TaskPlan(subtasks=[
        _st("st-13-1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/X.java"]),
    ])
    plan.shared_contract = {"dependencies": [
        {"module": "ruoyi-admin", "artifacts": ["ruoyi-alarm"]},
        {"module": "ruoyi-alarm", "artifacts": ["ruoyi-common"]},
    ]}
    mods = _plan_module_artifacts(plan)
    assert {"ruoyi-admin", "ruoyi-alarm"} <= mods, mods


def test_t2_prune_propagation_exempts_plan_module(tmp_path):
    """端到端：契约声明 ruoyi-admin→ruoyi-alarm（plan 新模块）→ 不进 pruned_artifacts。"""
    from swarm.brain.contract_utils import prune_contract_dependencies
    proj = _base_proj(tmp_path)
    plan = TaskPlan(subtasks=[
        _st("st-13-1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/X.java"]),
    ])
    plan.shared_contract = {"dependencies": [
        {"module": "ruoyi-admin", "artifacts": ["ruoyi-alarm", "ruoyi-framework"]},
        {"module": "ruoyi-alarm", "artifacts": ["ruoyi-common"]},
    ]}
    pruned = prune_contract_dependencies(plan, proj)
    assert "ruoyi-alarm" not in pruned.get("ruoyi-admin", []), pruned


# ── R67C-T5：依赖剔除同源传播到消费方子任务 desc ─────────────────────────────
def _patch_drop_submail(monkeypatch):
    import swarm.brain.maven_registry as mr

    def fake(pp, arts, idx=None, extra_module_artifacts=None):
        kept = [mr.ResolvedDep(group="x", artifact=str(a), version=None, source="baseline")
                for a in arts if "submail" not in str(a).lower()]
        dropped = [str(a) for a in arts if "submail" in str(a).lower()]
        return kept, dropped
    monkeypatch.setattr(mr, "resolve_artifacts", fake)


def test_t5_prune_propagates_to_consumer_desc(tmp_path, monkeypatch):
    """st-27-4 型：submail 被剔 → 提及它的子任务 desc 追加剔除通告。"""
    from swarm.brain.contract_utils import prune_contract_dependencies
    _patch_drop_submail(monkeypatch)
    proj = _base_proj(tmp_path)
    st = _st("st-27-4")
    st.description = "实现 VoiceNotifyService（Submail 语音 API，按渠道配置拨打）"
    plan = TaskPlan(subtasks=[st])
    plan.shared_contract = {"dependencies": [
        {"module": "ruoyi-alarm",
         "artifacts": ["com.github.submail:submail", "cn.hutool:hutool-all"]}]}
    prune_contract_dependencies(plan, proj)
    assert "R67C-T5" in plan.subtasks[0].description, plan.subtasks[0].description
    # 幂等：再跑一次不重复追加
    prune_contract_dependencies(plan, proj)
    assert plan.subtasks[0].description.count("R67C-T5") == 1


def test_t5_stale_marker_self_heals(tmp_path, monkeypatch):
    """hunter 二轮：某坐标本轮复原（不再被剔）→ 旧 T5 通告自动撤销（入口对称，非单向粘滞）。"""
    from swarm.brain.contract_utils import prune_contract_dependencies
    _patch_drop_submail(monkeypatch)     # 本轮只剔 submail；hutool 视为已复原
    proj = _base_proj(tmp_path)
    st = _st("st-x")
    st.description = ("实现 hutool 工具封装。\n【R67C-T5 依赖剔除通告】以下契约依赖无法确定性解析"
                     "坐标、已从构建与验收剔除：['hutool']。请勿 import/声明。")
    plan = TaskPlan(subtasks=[st])
    plan.shared_contract = {"dependencies": [
        {"module": "ruoyi-alarm", "artifacts": ["com.github.submail:submail"]}]}
    prune_contract_dependencies(plan, proj)
    assert "【R67C-T5" not in st.description, st.description   # 陈旧 hutool 通告已自愈撤销
    assert "hutool 工具封装" in st.description                # 正文保留


def test_t5_unrelated_subtask_not_annotated(tmp_path, monkeypatch):
    """desc 不提及被剔坐标的子任务 → 不追加通告。"""
    from swarm.brain.contract_utils import prune_contract_dependencies
    _patch_drop_submail(monkeypatch)
    proj = _base_proj(tmp_path)
    st = _st("st-15-1")
    st.description = "实现 AlarmRobot 实体与 Mapper"
    plan = TaskPlan(subtasks=[st])
    plan.shared_contract = {"dependencies": [
        {"module": "ruoyi-alarm", "artifacts": ["com.github.submail:submail"]}]}
    prune_contract_dependencies(plan, proj)
    assert "R67C-T5" not in plan.subtasks[0].description


# ── R67C-T6：desc 把实现留给后续/其他子任务 = 悬空占位孤岛 ─────────────────────
def test_t6_defer_to_sibling_warned():
    """st-35-1 型：desc 显式'留给后续子任务以占位预留' → warn 上报（不硬阻断可执行 plan）。"""
    st = _st("st-35-1")
    st.description = ("创建 AlarmEscalationJob 骨架，完成超时判定。本子任务只负责扫描+判定，"
                      "升级通知逻辑留给后续子任务以占位方法形式预留。")
    plan = TaskPlan(subtasks=[st])
    r = validate_module_coherence(plan)
    assert any("st-35-1" in w and "留给后续" in w for w in r.warnings), r.warnings
    assert not any("st-35-1" in i for i in r.issues), r.issues   # 只 warn 不进 issues


def test_t6_benign_reservation_not_flagged():
    """良性'预留扩展点/入口 + TODO'（非留给子任务）→ 不上报。"""
    st = _st("st-24-2")
    st.description = ("holiday_first 模式：预留节假日优先判定入口，可先按 rotation 兜底并留 "
                      "TODO 扩展点。")
    plan = TaskPlan(subtasks=[st])
    r = validate_module_coherence(plan)
    assert not any("st-24-2" in w for w in r.warnings), r.warnings


# ── R67C-T3a：纯资源/DDL 产物读代码=provenance 非 build 依赖，不补全图 fan-in ──
def test_t3a_pure_ddl_reading_code_not_wired():
    """st-13-2 型：create 只有 .sql、readable 一堆 .java → 不补 build 依赖边（防连坐）。"""
    from swarm.brain.contract_utils import wire_readable_provenance
    ddl = _st("st-ddl", create=["sql/alarm.sql"],
              readable=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java",
                        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmRobot.java"])
    e1 = _st("st-e1", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java"])
    e2 = _st("st-e2", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmRobot.java"])
    plan = TaskPlan(subtasks=[ddl, e1, e2])
    wire_readable_provenance(plan)
    assert (plan.subtasks[0].depends_on or []) == [], plan.subtasks[0].depends_on


def test_t3a_code_reading_code_still_wired():
    """代码消费者读代码 → 照常补 build 依赖边（不误伤真依赖）。"""
    from swarm.brain.contract_utils import wire_readable_provenance
    svc = _st("st-svc",
              create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"],
              readable=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java"])
    ent = _st("st-ent", create=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java"])
    plan = TaskPlan(subtasks=[svc, ent])
    wire_readable_provenance(plan)
    assert "st-ent" in (plan.subtasks[0].depends_on or []), plan.subtasks[0].depends_on


def test_t3a_pure_resource_reading_resource_still_wired():
    """纯资源读【资源】（.sql 依赖另一 .sql）→ 保留边（只跳过读代码的边）。"""
    from swarm.brain.contract_utils import wire_readable_provenance
    ddl2 = _st("st-ddl2", create=["sql/b.sql"], readable=["sql/a.sql"])
    ddl1 = _st("st-ddl1", create=["sql/a.sql"])
    plan = TaskPlan(subtasks=[ddl2, ddl1])
    wire_readable_provenance(plan)
    assert "st-ddl1" in (plan.subtasks[0].depends_on or []), plan.subtasks[0].depends_on


# ── R67C-T3b：混合 manifest 写者倒挂 → 拆早叶 pom-owner ─────────────────────
_FW_POM = "ruoyi-framework/pom.xml"
_FW_LOGIN = "ruoyi-framework/src/main/java/com/ruoyi/framework/shiro/service/SysLoginService.java"
_FW_GAUTH = "ruoyi-framework/src/main/java/com/ruoyi/framework/shiro/util/GoogleAuthUtils.java"


def test_t3b_split_mixed_manifest_owner_inversion():
    """st-7-2-1 型：写 framework pom+SysLoginService 且依赖同模块 GoogleAuthUtils 创建者→拆。"""
    from swarm.brain.contract_utils import split_manifest_owner_leaf
    w = _st("st-7-2-1", writable=[_FW_POM, _FW_LOGIN], depends=["st-7-1-1"])
    c = _st("st-7-1-1", create=[_FW_GAUTH], depends=["st-6"])
    plan = TaskPlan(subtasks=[w, c])
    plan.parallel_groups = [["st-7-1-1"], ["st-7-2-1"]]
    out = split_manifest_owner_leaf(plan)
    assert out and out[0]["leaf"] == "st-7-2-1-pom-ruoyi-framework", out
    _lid = "st-7-2-1-pom-ruoyi-framework"
    leaf = next(s for s in plan.subtasks if s.id == _lid)
    assert _FW_POM in (leaf.scope.writable or []) and (leaf.depends_on or []) == []
    assert _FW_POM not in (w.scope.writable or [])                 # W 交出 pom 写权
    assert _lid in (w.depends_on or [])                            # W 依赖早叶
    assert _lid in (c.depends_on or [])                            # 同模块编译者依赖早叶
    # 无成环：leaf 不可达 st-7-1-1
    assert "st-7-1-1" not in (leaf.depends_on or [])


def test_t3b_pure_manifest_owner_not_split():
    """纯 manifest 写者（无代码）→ 不拆（无成环之虞）。"""
    from swarm.brain.contract_utils import split_manifest_owner_leaf
    w = _st("st-scaf", writable=[_FW_POM], depends=["st-7-1-1"])
    c = _st("st-7-1-1", create=[_FW_GAUTH])
    plan = TaskPlan(subtasks=[w, c])
    out = split_manifest_owner_leaf(plan)
    assert out == []


def test_t3b_no_inversion_not_split():
    """混合写者但不依赖同模块编译者（无倒挂/成环）→ 不拆。"""
    from swarm.brain.contract_utils import split_manifest_owner_leaf
    w = _st("st-w", writable=[_FW_POM, _FW_LOGIN])   # 无 depends → 不依赖任何编译者
    c = _st("st-c", create=[_FW_GAUTH], depends=["st-w"])
    plan = TaskPlan(subtasks=[w, c])
    out = split_manifest_owner_leaf(plan)
    assert out == []


def test_t3b_no_edge_race_also_split():
    """hunter 二轮：W(混合)与同模块编译者 C【无 depends 边】(并行 race)→ 也拆（旧版只逮反向边漏此）。"""
    from swarm.brain.contract_utils import split_manifest_owner_leaf
    w = _st("st-w", writable=[_FW_POM, _FW_LOGIN])   # 无 depends
    c = _st("st-c", create=[_FW_GAUTH])              # 无 depends → 与 w 无序（race）
    plan = TaskPlan(subtasks=[w, c])
    out = split_manifest_owner_leaf(plan)
    assert out and out[0]["module"] == "ruoyi-framework", out
    assert out[0]["leaf"] in (c.depends_on or [])    # 编译者现依赖早叶（race 消除）


def test_t3b_multi_module_manifests_each_split(tmp_path):
    """ecc HIGH-2：一个 W 写两模块 manifest+各自倒挂 → 每模块各拆一叶（leaf-id 含 mod 不撞名）。"""
    from swarm.brain.contract_utils import split_manifest_owner_leaf
    _SYS_POM = "ruoyi-system/pom.xml"
    _SYS_HELPER = "ruoyi-system/src/main/java/com/ruoyi/system/SysHelper.java"
    _SYS_DEP = "ruoyi-system/src/main/java/com/ruoyi/system/SysDepCreator.java"
    w = _st("st-w", writable=[_FW_POM, _SYS_POM, _FW_LOGIN, _SYS_HELPER],
            depends=["st-c1", "st-c2"])
    c1 = _st("st-c1", create=[_FW_GAUTH])
    c2 = _st("st-c2", create=[_SYS_DEP])
    plan = TaskPlan(subtasks=[w, c1, c2])
    out = split_manifest_owner_leaf(plan)
    mods = {e["module"] for e in out}
    assert mods == {"ruoyi-framework", "ruoyi-system"}, out   # 两模块各拆一叶（旧版只拆首个）
    leaf_ids = {e["leaf"] for e in out}
    assert leaf_ids == {"st-w-pom-ruoyi-framework", "st-w-pom-ruoyi-system"}, leaf_ids
    # W 两个 pom 都交出
    assert _FW_POM not in (w.scope.writable or []) and _SYS_POM not in (w.scope.writable or [])
