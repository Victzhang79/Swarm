"""批次2 治本回归（deep_read_findings/21_full_sweep_20260727.md）：B-1/B-2/B-3/B-4/B-5/B-7。

B-1：_rebuild_plan 丢 plan 级账（finisher_attached/symbol_cycle_pairs）→ resplit 后 #6
覆盖配对键不等静默失效、成环对可同批并派白跑。修法=重建显式携带两账（revision 点同补）。

B-2：重复子任务 id 全链路无硬闸 → 同 id 并派 subtask_results last-wins 互相覆盖。修法=
validate_plan_structure 前置硬闸打回。

B-3：CVB 归位不清 AC/verify/desc 的 shadow 路径引用 → shadow 存在性断言确定性永假烧
L1 配额。修法=归位时引用改指 base 真身（③ sibling 同治，实体存活故改指非删除）。

B-4：R40-1 族（闸/attach）路径归一不剥 ./ → 假孤儿虚假外科。修法=四处统一 _norm_scope_path。

B-5：_DESC_ST_DEP_RE 只配中文"依赖 st-X"。修法=扩英文 depends on/requires/after。

B-7（疑点核验）：③c phys_roots 顶段排除可能漏判真路由——排除面改行为风险大于收益，
转可观测：跨簇共享的被排除 token 入 WARNING 观察账，不改判。
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan  # noqa: E402


def _st(sid: str, *, writable=None, create=None, desc="d", ac=None, depends=None) -> SubTask:
    return SubTask(
        id=sid, description=desc, difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(writable=writable or [], create_files=create or []),
        acceptance_criteria=ac or [], depends_on=depends or [],
    )


# ── B-1：_rebuild_plan 携带 plan 级账 ─────────────────────────────────────────

def test_b1_rebuild_plan_carries_plan_level_accounts():
    from swarm.brain.planning_nodes import _rebuild_plan

    old = TaskPlan(
        subtasks=[_st("st-1", create=["a/A.java"]), _st("st-2", create=["b/B.java"])],
        parallel_groups=[["st-1"], ["st-2"]],
        shared_contract={"x": 1},
        finisher_attached={"st-2": ["sql/orphan.sql"]},
        symbol_cycle_pairs=[["st-2", "st-1"]],
    )
    new = _rebuild_plan(old, [old.subtasks[0]])
    assert new.finisher_attached == {"st-2": ["sql/orphan.sql"]}, \
        "B-1：finisher_attached 必须随重建携带（#6 覆盖配对消费者）"
    assert new.symbol_cycle_pairs == [["st-2", "st-1"]], \
        "B-1：symbol_cycle_pairs 必须随重建携带（get_dispatch_batch 软序消费者）"
    assert new.shared_contract == {"x": 1}
    # 深拷贝：改新账不污染旧账
    new.finisher_attached["st-2"].append("z.sql")
    assert old.finisher_attached["st-2"] == ["sql/orphan.sql"]


# ── B-2：重复子任务 id 硬闸 ───────────────────────────────────────────────────

def test_b2_duplicate_subtask_id_rejected():
    from swarm.brain.plan_validator import validate_plan_structure

    plan = TaskPlan(
        subtasks=[_st("st-1", create=["a/A.java"]),
                  _st("st-1", create=["b/B.java"])],  # 同 id 双子任务
        parallel_groups=[["st-1"]],
    )
    res = validate_plan_structure(plan)
    assert not res.valid, "重复子任务 id 必须打回（last-wins 产出蒸发 fail-closed）"
    assert any("重复子任务 id" in str(i) for i in res.issues)


def test_b2_unique_ids_zero_regression():
    from swarm.brain.plan_validator import validate_plan_structure

    plan = TaskPlan(
        subtasks=[_st("st-1", create=["a/A.java"]), _st("st-2", create=["b/B.java"])],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    res = validate_plan_structure(plan)
    assert res.valid, f"正常计划不得被误杀: {res.issues}"


# ── B-5：依赖词元正则扩英文 ──────────────────────────────────────────────────

def test_b5_english_dependency_tokens_wired():
    from swarm.brain.plan_finisher import wire_described_dependency_tokens

    plan = TaskPlan(
        subtasks=[
            _st("st-1", create=["a/A.java"]),
            _st("st-2", create=["b/B.java"],
                desc="Implement controller, depends on st-1 pom setup"),
            _st("st-3", create=["c/C.java"], desc="Wire API after st-2"),
        ],
        parallel_groups=[["st-1"], ["st-2"], ["st-3"]],
    )
    added = wire_described_dependency_tokens(plan)
    by_id = {t.id: t for t in plan.subtasks}
    assert added.get("st-2") == ["st-1"] or "st-1" in (added.get("st-2") or []), \
        f"英文 depends on 必须成边: {added}"
    assert "st-1" in by_id["st-2"].depends_on
    assert "st-2" in by_id["st-3"].depends_on, "after st-X 同样成边"


# ── B-4：R40-1 闸 ./ 前缀口径同源 ────────────────────────────────────────────

def test_b4_r40_1_gate_dot_slash_no_false_orphan():
    """scope 写 ./x.java、file_plan 写 x.java（或反之）不得再判假孤儿。"""
    from swarm.brain.plan_validator import validate_file_plan_ownership

    plan = TaskPlan(
        subtasks=[_st("st-1", create=["./ruoyi-a/src/A.java"]),
                  _st("st-2", create=["ruoyi-b/src/B.java"])],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    fp = [{"path": "ruoyi-a/src/A.java"}, {"path": "./ruoyi-b/src/B.java"}]
    res = validate_file_plan_ownership(plan, fp)
    assert res.valid, f"./ 前缀漂移不得再假孤儿: {res.issues}"


# ── B-7：phys_roots 排除观察账（不改判，WARNING 留痕）────────────────────────

def test_b7_excluded_cross_cluster_route_logged(caplog):
    """顶段撞物理根的跨簇共享 token：不改判（不 REJECT）但 WARNING 观察账必在。"""
    from swarm.brain.plan_validator import _cross_cluster_route_double_claims

    # 两簇 handler 子任务：模块目录 ruoyi-a/ruoyi-b，desc 都声明 /ruoyi-a/notify
    # （顶段 ruoyi-a 撞物理根 → 排除；跨簇共享 → 观察账）
    h1 = _st("st-1", create=["ruoyi-a/src/main/java/com/x/NotifyController.java"],
             desc="新建 NotifyController 提供 POST /ruoyi-a/notify")
    h2 = _st("st-2", create=["ruoyi-b/src/main/java/com/y/AlarmController.java"],
             desc="新建 AlarmController 提供 POST /ruoyi-a/notify")
    plan = TaskPlan(subtasks=[h1, h2], parallel_groups=[["st-1"], ["st-2"]])
    with caplog.at_level(logging.WARNING):
        strong, weak = _cross_cluster_route_double_claims(plan)
    assert not strong, "B-7 不改判：phys_roots 排除面不复活 REJECT"
    assert any("B-7 观察账" in r.message for r in caplog.records), \
        "跨簇共享的被排除 token 必须 WARNING 留痕"


# ── B-3：CVB 归位清 AC/verify/desc shadow 引用 ───────────────────────────────

def test_b3_cvb_relocation_rewrites_shadow_refs(tmp_path):
    """信号1（file_plan modify 路径锚定）归位后：create→writable 指 base 真身，
    且 AC/verify/desc 里的 shadow 路径引用同步改指（治前=shadow 存在性断言确定性永假）。"""
    import subprocess

    from swarm.brain.contract_utils import deconflict_create_vs_base_modify_shadow
    from swarm.types import TaskHarness

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    base_p = "ruoyi-system/src/main/java/com/ruoyi/system/controller/GenController.java"
    shadow = "ruoyi-system/src/main/java/com/ruoyi/system/GenController.java"
    (root / base_p).parent.mkdir(parents=True)
    (root / base_p).write_text("// base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)

    st = _st(
        "st-1",
        create=[shadow],
        desc=f"新建代码生成控制器（落点 {shadow}）",
        ac=[f"test -f {shadow} 存在", "功能可编译"],
    )
    st.harness = TaskHarness(verify_commands=[f"test -f {shadow}"])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    fp = [{"path": shadow, "action": "modify"}]  # 信号1：落点本身被声明 modify

    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path=str(root))
    assert n == 1, "信号1 成立必须归位"
    assert st.scope.create_files == [], "shadow create 必须剥离"
    assert base_p in st.scope.writable, "base 真身必须入 writable(modify)"
    # B-3 核心：shadow 引用改指 base 真身（实体存活故改指非删除，③ sibling 同治）
    assert not any(shadow in str(a) for a in st.acceptance_criteria), \
        f"AC 不得残留 shadow 路径（存在性断言确定性永假）: {st.acceptance_criteria}"
    assert any(base_p in str(a) for a in st.acceptance_criteria), \
        "AC 引用必须改指 base 真身（验收意图保留）"
    assert "功能可编译" in st.acceptance_criteria, "无 shadow 引用的 AC 绝不动"
    assert not any(shadow in str(c) for c in st.harness.verify_commands)
    assert any(base_p in str(c) for c in st.harness.verify_commands)
    assert shadow not in st.description and base_p in st.description


def test_b3_module_prefix_collision_keeps_correct_base_ref(tmp_path):
    """批次2 闸门双 HIGH 红灯：模块根撞名（admin vs ruoyi-admin）下 shadow 是 base 后缀
    子串——AC 里【本已正确】的 base 引用必须原样保留（盲 replace 会绞成双前缀幽灵路径），
    shadow 引用改指 base，消费者子任务的 shadow 断言同样改指。"""
    import subprocess

    from swarm.brain.contract_utils import deconflict_create_vs_base_modify_shadow

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    base_p = "ruoyi-admin/src/main/java/com/ruoyi/GenController.java"
    shadow = "admin/src/main/java/com/ruoyi/GenController.java"
    assert shadow in base_p, "前提：shadow 必须是 base 的后缀子串（RuoYi 撞名形态）"
    (root / base_p).parent.mkdir(parents=True)
    (root / base_p).write_text("// base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)

    owner = _st("st-1", create=[shadow], desc="代码生成控制器",
                ac=[f"test -f {shadow} 存在",
                    f"test -f {base_p} 编译通过"])  # 本已正确的 base 引用
    consumer = _st("st-2", create=["ruoyi-admin/src/main/java/com/ruoyi/GenService.java"],
                   desc="服务层", ac=[f"test -f {shadow} 可用"])  # 消费者引用 shadow
    plan = TaskPlan(subtasks=[owner, consumer], parallel_groups=[["st-1"], ["st-2"]])
    fp = [{"path": shadow, "action": "modify"}]

    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path=str(root))
    assert n == 1
    # HIGH(a)：本已正确的 base 引用绝不被绞（ruoyi-ruoyi-admin 幽灵）
    assert f"test -f {base_p} 编译通过" in owner.acceptance_criteria, \
        f"正确的 base 引用必须原样保留: {owner.acceptance_criteria}"
    assert not any("ruoyi-ruoyi" in str(a) for a in owner.acceptance_criteria)
    # shadow 引用改指 base
    assert any(base_p in str(a) and "编译" not in str(a)
               for a in owner.acceptance_criteria), owner.acceptance_criteria
    # 闸门 M-1：消费者子任务的 shadow 断言同样改指（注意 shadow 是 base_p 后缀子串，
    # 改指后串仍含 shadow 子串——判据必须是有边界的完整 base_p 形态）
    assert consumer.acceptance_criteria == [f"test -f {base_p} 可用"], \
        f"消费者 shadow 断言必须改指 base: {consumer.acceptance_criteria}"


def test_b3_dot_slash_variant_no_double_replace(tmp_path):
    """reviewer HIGH 红灯：./ 前缀变体替换不得命中刚写入的 base（单遍 re.sub 不 rescan）。"""
    import subprocess

    from swarm.brain.contract_utils import deconflict_create_vs_base_modify_shadow

    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    base_p = "modules/admin/src/main/java/com/x/GenController.java"
    norm_shadow = "admin/src/main/java/com/x/GenController.java"
    raw_shadow = f"./{norm_shadow}"
    (root / base_p).parent.mkdir(parents=True)
    (root / base_p).write_text("// base\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=root, check=True)

    st = _st("st-1", create=[raw_shadow], desc="d",
             ac=[f"test -f {raw_shadow} 存在"])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    fp = [{"path": norm_shadow, "action": "modify"}]

    n = deconflict_create_vs_base_modify_shadow(plan, fp, project_path=str(root))
    assert n == 1
    assert st.acceptance_criteria == [f"test -f {base_p} 存在"], \
        f"不得出现 modules/modules 双写或 ./ 残留: {st.acceptance_criteria}"


def test_b4_trailing_slash_no_false_orphan():
    """hunter M-2 红灯：file_plan 尾斜杠 vs scope 无尾斜杠不得假孤儿。"""
    from swarm.brain.contract_utils import _norm_scope_path
    from swarm.brain.plan_validator import validate_file_plan_ownership

    assert _norm_scope_path("ruoyi-a/src/A.java/") == "ruoyi-a/src/A.java"
    plan = TaskPlan(
        subtasks=[_st("st-1", create=["ruoyi-a/src/A.java"]),
                  _st("st-2", create=["ruoyi-b/src/B.java"])],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    res = validate_file_plan_ownership(
        plan, [{"path": "ruoyi-a/src/A.java/"}, {"path": "ruoyi-b/src/B.java"}])
    assert res.valid, f"尾斜杠漂移不得假孤儿: {res.issues}"


def test_b5_thereafter_no_false_match():
    """reviewer LOW 红灯：thereafter st-1 不得误配（英文备选 \\b 左边界）。"""
    from swarm.brain.plan_finisher import wire_described_dependency_tokens

    plan = TaskPlan(
        subtasks=[_st("st-1", create=["a/A.java"]),
                  _st("st-2", create=["b/B.java"], desc="Ship thereafter st-1 wraps up")],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    added = wire_described_dependency_tokens(plan)
    assert not added, f"thereafter 不得成边: {added}"
    assert not plan.subtasks[1].depends_on


def test_b5_uppercase_id_wired():
    """hunter R2 LOW-2 红灯：IGNORECASE 捕到的 "Depends on ST-1" 必须落到真身 id 成边。"""
    from swarm.brain.plan_finisher import wire_described_dependency_tokens

    plan = TaskPlan(
        subtasks=[_st("st-1", create=["a/A.java"]),
                  _st("st-2", create=["b/B.java"], desc="Depends on ST-1 for the pom setup")],
        parallel_groups=[["st-1"], ["st-2"]],
    )
    added = wire_described_dependency_tokens(plan)
    assert plan.subtasks[1].depends_on == ["st-1"], \
        f"大写 ST-1 必须归一到真身 id 成边: {added}"


if __name__ == "__main__":
    test_b1_rebuild_plan_carries_plan_level_accounts()
    print("  ✅ B-1 _rebuild_plan 携带 plan 级账")
    test_b2_duplicate_subtask_id_rejected()
    test_b2_unique_ids_zero_regression()
    print("  ✅ B-2 重复子任务 id 硬闸（不误杀正常计划）")
    test_b5_english_dependency_tokens_wired()
    print("  ✅ B-5 英文依赖词元成边")
    test_b4_r40_1_gate_dot_slash_no_false_orphan()
    print("  ✅ B-4 R40-1 闸 ./ 口径同源不假孤儿")
    print("\n=== 批次2（B-7 观察账需 caplog，仅 pytest 跑）: 5/6 passed ===")
