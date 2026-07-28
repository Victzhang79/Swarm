"""R67L-B3（22号文批次3）行为测试：规划期考卷/事实源同源族六件套。

round67l 三路复盘定案的规划期治本：
  ① scope 写权语义与 file_plan action 对账（st-3-2 型 create 错声明 writable，11 处实锤）；
  ② file_plan 依赖边下推 depends_on（html→controller/domain→sql 断链，68 条实锤）；
  ③ intent 纯新建 scope 误标 MODIFY → CREATE（110/139 误标族）；
  ④ H3b 成环提示↔考卷正断言对账（st-2 卷子必死：提示"不得 import"vs 考卷必考 import）；
  ⑤ create-pom 裸奔闸最低验收强度（st-3-1 零闸过 parent 3.8.7 幻觉 pom）；
  ⑥ 禁令型未锚定子串 grep 语义闸（st-3 run-1 禁令散文撞禁令误杀）；
  ⑦（maven_registry）显式坐标 LLM 版本主张不得直采（st-2 整包旧版坐标对抗 SB4.0.6）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.brain.maven_registry as mr  # noqa: E402
from swarm.brain.contract_utils import correct_misclassified_intent  # noqa: E402
from swarm.brain.plan_finisher import (  # noqa: E402
    ensure_pom_create_min_acceptance,
    reconcile_scope_actions_with_file_plan,
    sanitize_negated_grep_exam,
    wire_file_plan_depends_edges,
    wire_symbol_consumption_edges,
)
from swarm.types import (FileScope, SubTask, SubTaskDifficulty, TaskHarness,  # noqa: E402
                         TaskIntent, TaskPlan)


def _st(sid, *, writable=None, create=None, intent=None, verify=None,
        desc="x", depends_on=None, ac=None):
    return SubTask(
        id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
        scope=FileScope(writable=writable or [], create_files=create or []),
        harness=TaskHarness(language="java", build_command="mvn -q compile",
                            verify_commands=verify or []),
        intent=intent or TaskIntent.MODIFY,
        depends_on=depends_on or [],
        acceptance_criteria=ac or [],
    )


def _plan(*sts):
    return TaskPlan(subtasks=list(sts))


# ─── ① scope↔file_plan action 对账 ───


def test_scope_action_create_in_writable_moved():
    """round67l st-3-2 原型：file_plan=create 落 writable → 归一到 create_files。"""
    st = _st("st-3-2", writable=["m/alarm.properties", "m/Keep.java"])
    plan = _plan(st)
    fp = [{"path": "m/alarm.properties", "action": "create"}]
    moved = reconcile_scope_actions_with_file_plan(plan, fp)
    assert moved == {"st-3-2": ["m/alarm.properties"]}
    assert st.scope.writable == ["m/Keep.java"]
    assert st.scope.create_files == ["m/alarm.properties"]


def test_scope_action_modify_in_create_moved_and_idempotent():
    st = _st("s1", create=["m/Old.java", "m/New.java"])
    plan = _plan(st)
    fp = [{"path": "m/Old.java", "action": "modify"}]
    moved = reconcile_scope_actions_with_file_plan(plan, fp)
    assert moved == {"s1": ["m/Old.java"]}
    assert st.scope.writable == ["m/Old.java"]
    assert st.scope.create_files == ["m/New.java"]
    # 幂等：再跑一次零变动
    assert reconcile_scope_actions_with_file_plan(plan, fp) == {}


def test_scope_action_no_fileplan_entry_untouched():
    st = _st("s1", writable=["m/X.java"])
    plan = _plan(st)
    assert reconcile_scope_actions_with_file_plan(plan, []) == {}
    assert st.scope.writable == ["m/X.java"]


# ─── ② file_plan 依赖边下推 ───


def test_file_plan_dep_edges_pushed_down():
    """round67l html→controller 断链原型：owner(html) 补边 owner(controller)。"""
    ctrl = _st("st-20", create=["m/AlarmTaskController.java"])
    html = _st("st-61-1", create=["admin/templates/task.html"])
    plan = _plan(ctrl, html)
    fp = [{"path": "admin/templates/task.html", "action": "create",
           "depends_on": ["m/AlarmTaskController.java"]}]
    added = wire_file_plan_depends_edges(plan, fp)
    assert added == {"st-61-1": ["st-20"]}
    assert html.depends_on == ["st-20"]
    # 幂等
    assert wire_file_plan_depends_edges(plan, fp) == {}


def test_file_plan_dep_edges_cycle_guard():
    """生产者已（传递）依赖消费者 → 成环跳过不猜（同 T4b 律）。"""
    a = _st("st-a", create=["m/A.java"], depends_on=["st-b"])
    b = _st("st-b", create=["m/B.java"])
    plan = _plan(a, b)
    fp = [{"path": "m/B.java", "action": "create", "depends_on": ["m/A.java"]}]
    added = wire_file_plan_depends_edges(plan, fp)
    assert added == {}, "st-b→st-a 会成环（st-a 已依赖 st-b）→ 必须跳过"
    assert b.depends_on == []


def test_file_plan_dep_edges_unowned_dep_skipped():
    a = _st("st-a", create=["m/A.java"])
    plan = _plan(a)
    fp = [{"path": "m/A.java", "action": "create", "depends_on": ["m/Ghost.java"]}]
    assert wire_file_plan_depends_edges(plan, fp) == {}
    assert a.depends_on == []


# ─── ③ intent 纯新建误标 MODIFY ───


def test_intent_pure_create_flips_to_create():
    st = _st("s1", create=["m/New.java"], intent=TaskIntent.MODIFY)
    plan = _plan(st)
    assert correct_misclassified_intent(plan) is True
    assert st.intent == TaskIntent.CREATE


def test_intent_mixed_scope_stays_modify():
    st = _st("s1", writable=["m/Old.java"], create=["m/New.java"],
             intent=TaskIntent.MODIFY)
    plan = _plan(st)
    assert correct_misclassified_intent(plan) is False
    assert st.intent == TaskIntent.MODIFY


def test_intent_audit_legacy_rules_intact():
    """回归：AUDIT 臂原语义不动。"""
    st = _st("s1", create=["m/New.java"], intent=TaskIntent.AUDIT)
    plan = _plan(st)
    assert correct_misclassified_intent(plan) is True
    assert st.intent == TaskIntent.CREATE


# ─── ④ H3b↔考卷正断言对账 ───


def _cycle_plan(consumer_verify):
    """构造 st-consumer 引用 st-producer 的类但 st-producer 依赖 st-consumer（成环）。"""
    producer = _st(
        "st-producer",
        create=["ruoyi-common/src/main/java/com/ruoyi/common/exception/user/TwoFactorException.java"],
        depends_on=["st-consumer"])
    consumer = _st(
        "st-consumer",
        create=["ruoyi-framework/src/main/java/com/ruoyi/framework/shiro/service/LoginService.java"],
        desc="使用 TwoFactorException 完成 2FA", verify=consumer_verify)
    return _plan(producer, consumer)


def test_h3b_positive_assertion_on_cycle_token_dropped():
    """st-2 原型：成环符号的 import/引用正断言与 H3b 提示打架 → 剔除。"""
    plan = _cycle_plan([
        "grep -n 'twoFactorCode' m/LoginService.java",
        "grep -n 'import com.ruoyi.common.exception.user.TwoFactorException' m/LoginService.java",
        "grep -n 'TwoFactorException' m/LoginService.java",
        "! grep -rn 'class TwoFactorException' m/",
    ])
    wire_symbol_consumption_edges(plan)
    vcs = plan.subtasks[1].harness.verify_commands
    assert any("twoFactorCode" in v for v in vcs), "无关正断言不动"
    assert any(v.strip().startswith("!") for v in vcs), "负断言（禁建同名类）与提示同向→保留"
    assert not any("import com.ruoyi.common.exception.user.TwoFactorException" in v
                   for v in vcs), "成环符号 import 正断言必须剔除"
    assert not any(
        (not v.strip().startswith("!")) and "TwoFactorException" in v for v in vcs), \
        "成环符号引用正断言必须剔除"


def test_h3b_no_cycle_keeps_exam():
    """无成环（边正常建立）→ 考卷原样不动。"""
    producer = _st(
        "st-producer",
        create=["ruoyi-common/src/main/java/com/ruoyi/common/exception/user/TwoFactorException.java"])
    consumer = _st(
        "st-consumer",
        create=["ruoyi-framework/src/main/java/com/ruoyi/framework/shiro/service/LoginService.java"],
        desc="使用 TwoFactorException", verify=["grep -n 'TwoFactorException' m/LoginService.java"])
    plan = _plan(producer, consumer)
    added = wire_symbol_consumption_edges(plan)
    assert added == {"st-consumer": ["st-producer"]}
    assert plan.subtasks[1].harness.verify_commands == [
        "grep -n 'TwoFactorException' m/LoginService.java"]


# ─── ⑤ create-pom 裸奔闸 ───


def test_naked_pom_create_gets_min_acceptance(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version></project>")
    st = _st("st-3-1", create=["ruoyi-alarm-interface/pom.xml"], verify=[])
    plan = _plan(st)
    injected = ensure_pom_create_min_acceptance(plan, str(tmp_path))
    assert "st-3-1" in injected
    vcs = st.harness.verify_commands
    assert any(v.startswith("test -f ruoyi-alarm-interface/pom.xml") for v in vcs)
    assert any("<version>${" in v for v in vcs), "parent 字面量断言"
    assert any("4.8.3" in v and "<parent>" in v for v in vcs), "parent 版本对账根版"


def test_pom_create_with_existing_exam_untouched(tmp_path):
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>g</groupId><artifactId>a</artifactId>"
        "<version>1.0</version></project>")
    st = _st("s1", create=["m/pom.xml"], verify=["grep -q 'x' m/pom.xml"])
    plan = _plan(st)
    assert ensure_pom_create_min_acceptance(plan, str(tmp_path)) == {}
    assert st.harness.verify_commands == ["grep -q 'x' m/pom.xml"]


def test_non_pom_create_untouched(tmp_path):
    st = _st("s1", create=["m/A.java"], verify=[])
    plan = _plan(st)
    assert ensure_pom_create_min_acceptance(plan, str(tmp_path)) == {}
    assert st.harness.verify_commands == []


def test_naked_root_pom_create_skips_parent_version_assertion(tmp_path):
    """复核 M-1：create【根 pom 自身】（无 <parent> 块）→ ③ parent 版本对账不得注入
    （注入即恒败冤杀），①② 仍在。"""
    (tmp_path / "pom.xml").write_text(
        "<project><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId>"
        "<version>4.8.3</version></project>")
    st = _st("st-root", create=["pom.xml"], verify=[])
    plan = _plan(st)
    injected = ensure_pom_create_min_acceptance(plan, str(tmp_path))
    assert "st-root" in injected
    vcs = st.harness.verify_commands
    assert any(v.startswith("test -f pom.xml") for v in vcs)
    assert any("<version>${" in v for v in vcs), "② 字面量断言仍在"
    assert not any("<parent>" in v for v in vcs), "根 pom 无 parent 块，③ 注入即冤杀"


def test_naked_pom_non_maven_stack_skipped(tmp_path):
    """复核 L-栈：基线为已知非 Maven 栈（npm manifest、无 pom.xml）→ plan 内 create-pom
    疑为 LLM 幻觉，整 pass 跳过不注入 Maven 专属断言（留 VALIDATE 打回）。"""
    (tmp_path / "package.json").write_text('{"name": "x", "version": "1.0.0"}')
    st = _st("s1", create=["pom.xml"], verify=[])
    plan = _plan(st)
    assert ensure_pom_create_min_acceptance(plan, str(tmp_path)) == {}
    assert st.harness.verify_commands == []


# ─── ⑥ 禁令 grep 语义闸 ───


def test_negated_pom_bareword_rewritten_to_artifact_tag():
    """st-14 原型：! grep -qi 'lombok' pom → artifactId 标签形（注释散文豁免）。"""
    st = _st("st-14", create=["m/pom.xml"],
             verify=["! grep -qi 'lombok' ruoyi-alarm/pom.xml",
                     "grep -q '<artifactId>ruoyi-common</artifactId>' ruoyi-alarm/pom.xml"])
    plan = _plan(st)
    out = sanitize_negated_grep_exam(plan)
    assert "st-14" in out
    vcs = st.harness.verify_commands
    assert vcs[0] == "! grep -qi '<artifactId>lombok</artifactId>' ruoyi-alarm/pom.xml"
    assert vcs[1].startswith("grep -q"), "正断言不动"


def test_negated_java_package_rewritten_to_anchored_import():
    st = _st("st-43", create=["m/A.java"],
             verify=["! grep -rn 'javax\\.' ruoyi-alarm/src/main/java/com/ruoyi/alarm/api/"])
    plan = _plan(st)
    out = sanitize_negated_grep_exam(plan)
    assert "st-43" in out
    assert st.harness.verify_commands[0] == (
        "! grep -rn '^[[:space:]]*import[[:space:]].*javax\\.' "
        "ruoyi-alarm/src/main/java/com/ruoyi/alarm/api/")


def test_negated_prose_and_compound_untouched():
    """含空格散文 pattern / 非良性复合命令 / 已锚定 → 原样保留（不猜语义）。"""
    v1 = "! grep -rn 'class TwoFactorException' ruoyi-framework/src/"
    v2 = "grep -E '@Aspect' a.java && ! grep -n 'javax.servlet' b.java"
    v3 = "! grep -q '^[[:space:]]*import[[:space:]].*lombok' c.java"
    st = _st("s1", create=["m/A.java"], verify=[v1, v2, v3])
    plan = _plan(st)
    assert sanitize_negated_grep_exam(plan) == {}
    assert st.harness.verify_commands == [v1, v2, v3]


def test_negated_benign_echo_suffix_preserved_on_rewrite():
    """st-14 真杀形态：`! grep … && echo NO_LOMBOK` 良性后缀剥离重写再拼回。"""
    st = _st("st-14", create=["m/pom.xml"],
             verify=["! grep -qi 'lombok' ruoyi-alarm/pom.xml && echo NO_LOMBOK",
                     "! grep -qi 'spring-boot-starter-security' ruoyi-alarm/pom.xml && echo NO_SPRING_SECURITY"])
    plan = _plan(st)
    out = sanitize_negated_grep_exam(plan)
    assert "st-14" in out
    vcs = st.harness.verify_commands
    assert vcs[0] == ("! grep -qi '<artifactId>lombok</artifactId>' ruoyi-alarm/pom.xml"
                      " && echo NO_LOMBOK")
    assert vcs[1] == ("! grep -qi '<artifactId>spring-boot-starter-security</artifactId>'"
                      " ruoyi-alarm/pom.xml && echo NO_SPRING_SECURITY")


def test_negated_quoted_echo_and_printf_suffix_rewritten():
    """复核 L-4：引号 echo / printf 后缀同属良性可剥离形态（不再漏网留误杀）。"""
    st = _st("st-q", create=["m/pom.xml"],
             verify=['! grep -qi \'lombok\' m/pom.xml && echo "NO_LOMBOK"',
                     "! grep -qi 'guava' m/pom.xml && printf OK"])
    plan = _plan(st)
    out = sanitize_negated_grep_exam(plan)
    assert "st-q" in out
    vcs = st.harness.verify_commands
    assert vcs[0] == ('! grep -qi \'<artifactId>lombok</artifactId>\' m/pom.xml'
                      ' && echo "NO_LOMBOK"')
    assert vcs[1] == ("! grep -qi '<artifactId>guava</artifactId>' m/pom.xml && printf OK")


def test_negated_classname_bareword_not_weakened_to_import_anchor():
    """复核 M-2（reviewer）：大写类名禁令（Lombok）不是包形词——锚定 import 会把
    "不得使用"弱化成"不得 import"（new Lombok() 内联漏网）→ 保守不动。"""
    v1 = "! grep -rn 'Lombok' ruoyi-alarm/src/main/java/"
    v2 = "! grep -rn 'TwoFactorException' ruoyi-alarm/src/main/java/"
    st = _st("s1", create=["m/A.java"], verify=[v1, v2])
    plan = _plan(st)
    assert sanitize_negated_grep_exam(plan) == {}
    assert st.harness.verify_commands == [v1, v2]


def test_finish_pass_crash_marks_failed_machine_readable(monkeypatch):
    """终扫整改：finish 各确定性 pass 崩溃必须落 *_failed 机读标记（通用扫尾进
    degraded_reasons）——崩溃≠零命中可区分，绝不只剩无人 grep 的 WARNING。"""
    import swarm.brain.plan_finisher as pf
    st = _st("s1", create=["m/A.java"], verify=["grep -q 'x' m/A.java"])
    plan = _plan(st)
    monkeypatch.setattr(pf, "wire_symbol_consumption_edges",
                        lambda _p: (_ for _ in ()).throw(RuntimeError("boom")))
    out = pf.finish_plan_deterministic(plan, None)
    assert out.get("symbol_consumption_edges_failed") is True, out


# ─── ⑦ 显式坐标 LLM 版本主张不得直采（maven_registry）───


def _idx(**kw):
    base = dict(known=True, managed_complete=True, project_group="com.ruoyi",
                module_artifacts={"ruoyi-common"}, managed={}, dep_groups={},
                bom_imports=[])
    base.update(kw)
    return mr.BaselineIndex(**base)


def test_explicit_managed_version_stripped(monkeypatch):
    """st-2 原型：aop:3.5.16 显式版本 vs BOM 受管 → 剥版本（受管权威优先）。"""
    monkeypatch.setattr(mr, "_lookup_enabled", lambda: False)
    idx = _idx(managed={"spring-boot-starter-aop": "org.springframework.boot"})
    kept, dropped = mr.resolve_artifacts(
        "/x", ["org.springframework.boot:spring-boot-starter-aop:3.5.16"], idx=idx)
    assert dropped == []
    assert kept[0].version is None, "受管依赖必须剥掉 LLM 版本主张"
    assert kept[0].group == "org.springframework.boot"


def test_explicit_hallucinated_version_corrected(monkeypatch):
    """幻觉版本（仓库确证查无）→ 校正到最新稳定版。"""
    monkeypatch.setattr(mr, "registry_version_exists", lambda g, a, v: False)
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: "2.0.65")
    idx = _idx()
    kept, dropped = mr.resolve_artifacts(
        "/x", ["com.alibaba.fastjson2:fastjson2:9.9.9"], idx=idx)
    assert dropped == []
    assert kept[0].version == "2.0.65"


def test_explicit_hallucinated_version_no_stable_dropped(monkeypatch):
    """幻觉版本且无可用稳定版 → 如实丢弃（绝不逼 worker 臆造）。"""
    monkeypatch.setattr(mr, "registry_version_exists", lambda g, a, v: False)
    monkeypatch.setattr(mr, "registry_latest_version", lambda g, a: None)
    idx = _idx()
    kept, dropped = mr.resolve_artifacts("/x", ["com.x:y:9.9.9"], idx=idx)
    assert dropped == ["com.x:y:9.9.9"] and kept == []


def test_explicit_version_unreachable_kept_failopen(monkeypatch):
    """仓库不可达=证据缺失 → fail-open 保留（R56-6，L1 同族规则兜底）。"""
    monkeypatch.setattr(mr, "registry_version_exists", lambda g, a, v: None)
    idx = _idx()
    kept, dropped = mr.resolve_artifacts("/x", ["com.x:y:1.2.3"], idx=idx)
    assert dropped == [] and kept[0].version == "1.2.3"


def test_explicit_reactor_module_pinned_project_version(monkeypatch):
    idx = _idx(module_artifacts={"ruoyi-alarm"})
    kept, dropped = mr.resolve_artifacts("/x", ["com.ruoyi:ruoyi-alarm:9.9.9"], idx=idx)
    assert dropped == []
    assert kept[0].version == "${project.version}", "reactor 兄弟钉 ${project.version}"
