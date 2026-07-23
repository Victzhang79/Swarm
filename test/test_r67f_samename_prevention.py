"""R67F 同名异包 create 三层预防红测试（round67f task=ad7b1916 复盘定案）。

死因：同名(simple name)JVM 类被多子任务在【不同包】各自 create（异 FQN）——
  · st-27-1 create com/ruoyi/alarm/util/AesUtils.java 与 st-6 create
    com/ruoyi/common/utils/encrypt/AesUtils.java（Spring bean 名默认 simple name，两份并存启动即冲突）。
  G1 ③b(R67-T1b) 正确 REJECT，但纯打回→LLM 全量重拆→renumber 后同接口原样重犯（轮1 st-27-1 /
  轮2 st-11-1）→无限烧（k3 连烧 2 轮同类重犯，用户手动取消）。

三层治本：
  层③ deconflict_same_name_cross_package_creates —— 契约 defined_in 有唯一权威 owner 时确定性消解
    （异包副本剥除+改 readable+依赖 owner），无权威者仍留 G1 ③b REJECT（fail-closed）。
  层② _samename_violation_signature —— 去 st-id 的规范化违例签名（Σ basename+包对集合），
    连续两轮不变即熔断（现有 R64-T3 整份 issue 文本签名因 st-id churn 逮不住）。
  层① prompt 栈感知硬约束 + 契约 constants 补 defined_in（在别处测/属规划期 LLM 引导）。
"""
from __future__ import annotations

from swarm.brain.contract_utils import (
    deconflict_same_name_cross_package_creates,
    resolve_plan_conflicts,
)
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan

_ALARM_AES = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/AesUtils.java"
_COMMON_AES = "ruoyi-common/src/main/java/com/ruoyi/common/utils/encrypt/AesUtils.java"


def _st(sid, *, create=None, writable=None, readable=None, depends=None,
        ac=None, verify=None, desc="d", lang="java"):
    return SubTask(
        id=sid, description=desc,
        scope=FileScope(writable=writable or [], create_files=create or [], readable=readable or []),
        harness=TaskHarness(language=lang, verify_commands=verify or []),
        acceptance_criteria=ac or [], depends_on=depends or [],
    )


def _contract(defined_in, name="AesUtils"):
    return {"interfaces": [{"name": name, "defined_in": defined_in}]}


# ── 层③ 主解：契约有唯一权威 owner → 确定性消解 ─────────────────────────────────
def test_l3_strips_wrong_package_dup_when_contract_owns():
    owner = _st("st-owner", create=[_COMMON_AES])
    dup = _st("st-dup", create=[_ALARM_AES],
              ac=[f"{_ALARM_AES} 按用途实现并编译通过", "无关验收保留"],
              verify=[f"javac {_ALARM_AES}"])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract=_contract(_COMMON_AES))
    n = deconflict_same_name_cross_package_creates(plan)
    assert n == 1, "契约有 owner 时未消解同名异包副本"
    # 异包副本被剥除
    assert _ALARM_AES not in (dup.scope.create_files or [])
    # 三面同步：AC/verify 中针对被剥文件（全路径）的条目清掉，无关条目保留
    assert not any(_ALARM_AES in str(a) for a in (dup.acceptance_criteria or []))
    assert "无关验收保留" in dup.acceptance_criteria
    assert not any(_ALARM_AES in str(v) for v in (dup.harness.verify_commands or []))
    # 消费方改 readable 指向 owner 真实落点 + 依赖 owner
    assert any("AesUtils" in r for r in (dup.scope.readable or []))
    assert "st-owner" in (dup.depends_on or [])
    # owner 侧不动
    assert _COMMON_AES in owner.scope.create_files


def test_l3_no_contract_authority_left_untouched():
    """无契约权威（纯常量类等不在 interfaces）→ 不动，留 G1 ③b REJECT（fail-closed 绝不静默挑边）。"""
    a = _st("st-a", create=[_ALARM_AES])
    b = _st("st-b", create=[_COMMON_AES])
    plan = TaskPlan(subtasks=[a, b], shared_contract={})
    n = deconflict_same_name_cross_package_creates(plan)
    assert n == 0
    assert _ALARM_AES in a.scope.create_files and _COMMON_AES in b.scope.create_files


def test_l3_authority_owner_not_created_left_untouched():
    """契约声明 owner 包 X，但两创建者都在别的包（无人创建 owner FQN）→ fail-closed 不动。"""
    a = _st("st-a", create=[_ALARM_AES])
    b = _st("st-b", create=["ruoyi-admin/src/main/java/com/ruoyi/web/util/AesUtils.java"])
    plan = TaskPlan(subtasks=[a, b], shared_contract=_contract(_COMMON_AES))  # owner 落点无人建
    n = deconflict_same_name_cross_package_creates(plan)
    assert n == 0
    assert _ALARM_AES in a.scope.create_files


def test_l3_go_same_basename_not_touched():
    """非 JVM：Go 同名文件跨模块合法（import 由模块限定）——栈中立绝不误伤。"""
    a = _st("st-a", create=["svc-a/internal/handler/handler.go"], lang="go")
    b = _st("st-b", create=["svc-b/internal/handler/handler.go"], lang="go")
    plan = TaskPlan(subtasks=[a, b],
                    shared_contract={"interfaces": [{"name": "handler",
                                                     "defined_in": "svc-a/internal/handler/handler.go"}]})
    n = deconflict_same_name_cross_package_creates(plan)
    assert n == 0
    assert "svc-a/internal/handler/handler.go" in a.scope.create_files
    assert "svc-b/internal/handler/handler.go" in b.scope.create_files


def test_l3_test_layout_same_basename_not_touched():
    """每模块一个 ApplicationTests 是生态惯例（test classpath 每模块独立）——绝不误杀。"""
    a = _st("st-a", create=["mod-a/src/test/java/com/a/ApplicationTests.java"])
    b = _st("st-b", create=["mod-b/src/test/java/com/b/ApplicationTests.java"])
    plan = TaskPlan(subtasks=[a, b], shared_contract={
        "interfaces": [{"name": "ApplicationTests",
                        "defined_in": "mod-a/src/test/java/com/a/ApplicationTests.java"}]})
    n = deconflict_same_name_cross_package_creates(plan)
    assert n == 0


def test_l3_same_fqn_cross_module_not_handled_here():
    """同 FQN 跨物理模块（相同包不同根）由 ③(#101) 处理——本 pass 判据是异 FQN，len(fqns)<2 跳过。"""
    a = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
    b = "ruoyi-admin/src/main/java/com/ruoyi/alarm/appkey/domain/AlarmAppSecret.java"
    sa = _st("st-a", create=[a])
    sb = _st("st-b", create=[b])
    plan = TaskPlan(subtasks=[sa, sb], shared_contract={
        "interfaces": [{"name": "AlarmAppSecret", "defined_in": a}]})
    n = deconflict_same_name_cross_package_creates(plan)
    assert n == 0, "同 FQN 跨模块组不应被本 pass 处理（避免与 ③ 双改）"
    assert a in sa.scope.create_files and b in sb.scope.create_files


def test_l3_idempotent():
    owner = _st("st-owner", create=[_COMMON_AES])
    dup = _st("st-dup", create=[_ALARM_AES])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract=_contract(_COMMON_AES))
    assert deconflict_same_name_cross_package_creates(plan) == 1
    assert deconflict_same_name_cross_package_creates(plan) == 0, "二次运行应幂等（无残留可消解）"


def test_l3_wired_into_resolve_plan_conflicts():
    """层③ 必须挂进 resolve_plan_conflicts（ELABORATE 每轮跑，G1 前消解）。"""
    owner = _st("st-owner", create=[_COMMON_AES])
    dup = _st("st-dup", create=[_ALARM_AES])
    plan = TaskPlan(subtasks=[owner, dup], shared_contract=_contract(_COMMON_AES))
    counts = resolve_plan_conflicts(plan)
    assert counts.get("samename_creates_deconflicted") == 1, \
        "resolve_plan_conflicts 未接线层③（同名异包消解计数缺失）"
    assert _ALARM_AES not in (dup.scope.create_files or [])


# ── 层② 去 st-id 规范化签名（R64-T3 收敛熔断对全量重拆 renumber 免疫）────────────
from swarm.brain.plan_validator import normalize_structural_signature  # noqa: E402


def _samename_issue(base, pkg_a, pkg_b, id_a, id_b):
    """仿 G1 ③b 违例文本（内嵌子任务 id，随重拆 renumber churn）。"""
    return (f"同名类 '{base}' 被多个子任务在不同包各自 create："
            f"{pkg_a}（{id_a}）; {pkg_b}（{id_b}）。请裁决唯一 owner。")


def test_l2_signature_renumber_invariant():
    """k3 实锤：同一逻辑违例两轮 st-id churn（st-27-1→st-11-1）→ 规范化后签名一致。"""
    r1 = [_samename_issue("aesutils.java", "com/ruoyi/alarm/util/AesUtils.java",
                          "com/ruoyi/common/utils/encrypt/AesUtils.java", "st-27-1", "st-6")]
    r2 = [_samename_issue("aesutils.java", "com/ruoyi/alarm/util/AesUtils.java",
                          "com/ruoyi/common/utils/encrypt/AesUtils.java", "st-11-1", "st-5")]
    assert normalize_structural_signature(r1) == normalize_structural_signature(r2), \
        "去 st-id 规范化后同一逻辑违例两轮签名应一致（否则熔断被 renumber 击穿）"
    # 反例：原始整份文本签名【不】一致（正是 R64-T3 被击穿之处）
    assert sorted(str(i) for i in r1) != sorted(str(i) for i in r2)


def test_l2_signature_distinguishes_different_violations():
    """不同 basename/包对 → 规范化签名仍不同（不过度熔断，防误杀真收敛）。"""
    a = [_samename_issue("aesutils.java", "com/a/AesUtils.java", "com/b/AesUtils.java", "st-1", "st-2")]
    b = [_samename_issue("alarmconstants.java", "com/a/AlarmConstants.java",
                         "com/c/AlarmConstants.java", "st-1", "st-2")]
    assert normalize_structural_signature(a) != normalize_structural_signature(b)


def test_l2_signature_route_text_not_collapsed():
    """★复核 Hunter#2 整改★：不同路由违例（/api/first-page vs /api/first-detail）文本内含 'st-'
    子串，若正则无左边界锚会误规范成同一 'first-*' 签名→误熔断真收敛 plan。锚后须区分。"""
    a = ["HTTP 路由 '/api/first-page' 被多个子任务各自新建的路由处理器重复声明：st-3, st-7"]
    b = ["HTTP 路由 '/api/first-detail' 被多个子任务各自新建的路由处理器重复声明：st-4, st-9"]
    assert normalize_structural_signature(a) != normalize_structural_signature(b), \
        "不同路由违例被误规范成同一签名（正则吞了 first-page 的 st- 子串）"
    # 但真实 st-id churn（同路由两轮 renumber）仍应规范为一致
    c = ["HTTP 路由 '/api/first-page' 被多个子任务各自新建的路由处理器重复声明：st-11, st-22"]
    assert normalize_structural_signature(a) == normalize_structural_signature(c)


def test_l2_signature_shrinking_set_not_fused():
    """违例集缩小（层③消解掉一个）→ 签名不同 → 不熔断（正确识别进展）。"""
    both = [_samename_issue("aesutils.java", "com/a/AesUtils.java", "com/b/AesUtils.java", "st-1", "st-2"),
            _samename_issue("alarmconstants.java", "com/a/AlarmConstants.java",
                            "com/c/AlarmConstants.java", "st-3", "st-4")]
    residual = [_samename_issue("alarmconstants.java", "com/a/AlarmConstants.java",
                                "com/c/AlarmConstants.java", "st-9", "st-9-1")]
    assert normalize_structural_signature(both) != normalize_structural_signature(residual)


def test_l2_signature_empty_and_idempotent():
    assert normalize_structural_signature([]) == []
    assert normalize_structural_signature(None) == []
    sig = normalize_structural_signature([_samename_issue(
        "x.java", "com/a/X.java", "com/b/X.java", "st-1", "st-2")])
    assert normalize_structural_signature([_samename_issue(
        "x.java", "com/a/X.java", "com/b/X.java", "st-1", "st-2")]) == sig


# ── 层② fan-out 前硬预算禁写清单（contract_owner_ledger_block）─────────────────
from swarm.brain.contract_utils import contract_owner_ledger_block  # noqa: E402


def test_l2_ledger_lists_jvm_claimed_owners():
    block = contract_owner_ledger_block(_contract(_COMMON_AES))
    assert "AesUtils" in block
    assert "com/ruoyi/common/utils/encrypt/AesUtils.java" in block
    assert "唯一 owner" in block and "严禁" in block


def test_l2_ledger_empty_for_no_jvm_symbols():
    # 非 JVM 契约（Go 路径）→ 空串（栈中立，不污染非 JVM prompt）。
    block = contract_owner_ledger_block(
        {"interfaces": [{"name": "handler", "defined_in": "svc-a/internal/handler/handler.go"}]})
    assert block == ""


def test_l2_ledger_empty_for_no_contract():
    assert contract_owner_ledger_block({}) == ""
    assert contract_owner_ledger_block(None) == ""
    assert contract_owner_ledger_block({"interfaces": []}) == ""


def test_l2_ledger_dedup_by_basename():
    c = {"interfaces": [
        {"name": "AesUtils", "defined_in": _COMMON_AES},
        {"name": "AesUtils2", "defined_in": _COMMON_AES},  # 同 basename 只列一次
    ]}
    block = contract_owner_ledger_block(c)
    assert block.count("→ 唯一 owner") == 1  # 同 basename 只列一行
