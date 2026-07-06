"""CODEWALK 根因C（批4b）：规划层残留 Java/RuoYi 特化，抽成通用规律/栈驱动。

① plan_batch._infer_group_from_path：硬编 ruoyi/ruoyi-system 专名 → 换两条通用规律
   （`<前缀>-<通用后缀>` 聚合模块名 + Java 包根后的 groupId 段），RuoYi 行为不变、
   其它项目同等受益。
② contract_utils 注入文案 "照此 RuoYi 写法" → 项目无关表述。
③ TECH_DESIGN prompt 内嵌 E2E 项目名 alarm-task/alarm-channel → 通用示例。
④ _split_oversized_by_files 验收写死 mvn compile → 从 harness.build_command 取（栈感知）。
⑤ _infer_create_layer 仅 Java → 补 Vue/TS/Go/Py 常见分层（识别不了仍 fail-safe None）。
"""
from __future__ import annotations

import pathlib

from swarm.brain.contract_utils import _infer_create_layer
from swarm.brain.plan_batch import _infer_group_from_path

_BRAIN = pathlib.Path(__file__).resolve().parent.parent / "brain"


# ── ① 分组推断：RuoYi 行为保持 + 非 RuoYi 项目受益 ─────────────────────────
def test_group_ruoyi_path_behavior_unchanged():
    assert _infer_group_from_path(
        "ruoyi-system/src/main/java/com/ruoyi/alarm/task/AlarmTask.java"
    ) == "alarm/task"


def test_group_generic_project_module_prefix():
    """任意项目的 <前缀>-common 聚合模块名与 groupId 段都应视作脚手架段。"""
    assert _infer_group_from_path(
        "acme-common/src/main/java/com/acme/billing/BillingService.java"
    ) == "billing"


def test_group_other_project_aggregate_modules_also_generic():
    """硬编 ruoyi-* 专名已删——通用规律必须让任意项目的聚合模块名同样被视作脚手架段
    （旧代码此路径会把 shopx-framework 当业务段返回）。"""
    assert _infer_group_from_path(
        "shopx-framework/src/main/java/org/shopx/pay/refund/RefundService.java"
    ) == "pay/refund"


def test_group_microservice_names_not_swallowed():
    """hunter：payment-service 这类微服务【业务名】不得被聚合模块正则吞掉
    （后缀名单刻意不含 service/api/app/biz/web，且只匹配根级段）。"""
    g = _infer_group_from_path("payment-service/api/v1/handler.go")
    assert "payment-service" in g, f"微服务名应保留为业务分组段: {g}"
    # 深层同形名（services/billing-service/…）也不吞
    g2 = _infer_group_from_path("services/billing-service/handler/user.go")
    assert "billing-service" in g2, g2


def test_group_business_dir_named_io_not_pkgroot():
    """hunter：业务目录恰叫 io（路径首段）时不得按 Java 包根处理其子目录。"""
    g = _infer_group_from_path("io/user/UserService.java")
    assert "user" in g, f"io 在首段非包根，user 应算业务段: {g}"


# ── ②③ 产品 prompt/注入文案不得含 E2E 项目专名 ────────────────────────────
def test_no_e2e_project_names_in_planning_prompts():
    for fname in ("planning_nodes.py", "contract_utils.py"):
        src = (_BRAIN / fname).read_text()
        for token in ("alarm-task", "alarm-channel", "照此 RuoYi"):
            assert token not in src, f"{fname} 含 E2E 项目专名/特化文案: {token}"


# ── ④ 拆分验收命令栈感知 ──────────────────────────────────────────────────
def _oversized_subtask(build_command: str):
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskHarness

    # 4 个实体 × 3 文件——拆分器按实体词干打包，需可区分的实体名才会拆
    files = [f"svc/internal/{e}/{e.capitalize()}{suffix}.go"
             for e in ("user", "order", "item", "report")
             for suffix in ("Service", "Handler", "Repo")]
    return SubTask(
        id="st-big", description="大子任务", difficulty=SubTaskDifficulty.MEDIUM,
        modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=files),
        harness=TaskHarness(build_command=build_command),
    )


def test_split_acceptance_uses_harness_build_command():
    from swarm.brain.planning_nodes import _split_oversized_by_files

    children = _split_oversized_by_files(_oversized_subtask("go build ./..."), max_files=4)
    assert len(children) >= 2, "应触发拆分"
    acc = " ".join(a for c in children for a in (c.acceptance_criteria or []))
    assert "go build ./..." in acc, f"验收应带 harness 构建命令: {acc}"
    assert "mvn compile" not in acc


def test_split_acceptance_no_hardcoded_mvn_when_harness_empty():
    from swarm.brain.planning_nodes import _split_oversized_by_files

    children = _split_oversized_by_files(_oversized_subtask(""), max_files=4)
    assert len(children) >= 2
    acc = " ".join(a for c in children for a in (c.acceptance_criteria or []))
    assert "mvn compile" not in acc, "harness 无命令时不得回退写死 mvn"
    assert "构建通过" in acc or "编译" in acc


# ── ⑤ _infer_create_layer 多栈 ────────────────────────────────────────────
def test_infer_layer_java_unchanged():
    assert _infer_create_layer("m/src/main/java/com/x/controller/AController.java") == \
        ("controller", "**/controller/*.java")


def test_infer_layer_non_java_stacks():
    assert _infer_create_layer("web/src/views/user/index.vue")[0] == "vue_view"
    assert _infer_create_layer("web/src/components/UserCard.vue")[0] == "vue_component"
    assert _infer_create_layer("web/src/api/user.ts")[0] == "api_client"
    assert _infer_create_layer("svc/internal/handler/user.go")[0] == "go_handler"
    assert _infer_create_layer("app/routers/user.py")[0] == "py_router"


def test_infer_layer_unknown_still_failsafe_none():
    assert _infer_create_layer("docs/design.md") is None
    assert _infer_create_layer("svc/internal/repo/user.go") is None
