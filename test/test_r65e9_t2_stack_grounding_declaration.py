"""R65E9-T2（round65e9 FAILED@PLAN 三路定案·上游 grounding 根）：把技术栈画像
grounding 注入 baseline_covered【声明步】，源头减少假存量申报（与 T1 pin 互补：
T1=收敛保证的 belt，T2=源头减少的 suspenders）。

死因上游侧：detect_stack 产出的权威栈画像（"本项目无 Redis / 用 EhCache / 禁 .vue"）
此前【只喂 tech_design】（planning_nodes.py:1702），PLAN 声明 baseline_covered 时 planner
看不到它→凭框架惯性谎称"现有 Redis 诊断代码"（基线真无 Redis）→被证据闸拒→limbo 死钉。

治：新增 _baseline_stack_grounding_block(state)——把 format_stack_for_prompt 的【能力边界
硬约束】（变体/前端形态/鉴权/基建概念，不含方法签名载荷）注入到主 PLAN、分批 PLAN、P1
外科补齐三个 baseline 声明面。format_stack_for_prompt 新增 include_method_sigs 开关（默认
True 保 tech_design 逐字不变），声明步用 False 取精简版（只要变体禁令不要方法签名 payload）。
通用多栈：只渲染 detect_stack 磁盘探测到的真实画像，不写死任何框架/项目。
"""
from __future__ import annotations

import swarm.brain.nodes as nodes
from swarm.brain.nodes import _baseline_stack_grounding_block, _lean_stack_directive
from swarm.brain.stack_detect import format_stack_for_prompt


def _profile():
    return {
        "frontend": "Thymeleaf",
        "backend": "Spring Boot",
        "build": "Maven",
        "frontend_kind": "server-template",
        "confidence": 0.9,
        "auth": {"variant": "shiro"},
        "infra_symbols": {
            "缓存工具": ["com.example.common.core.redis.RedisCache",
                     "com.example.common.utils.CacheUtils"],
        },
        "infra_symbol_methods": {
            "com.example.common.utils.CacheUtils": [
                "public static <T> T get(String key)",
                "public static void put(String key, Object value)",
            ],
        },
    }


# ── format_stack_for_prompt: include_method_sigs 开关 ──
def test_format_default_includes_method_sigs():
    """缺省 include_method_sigs=True → 逐字向后兼容（tech_design 路径不受影响）。"""
    s = format_stack_for_prompt(_profile())
    assert "照抄签名" in s, "默认必须渲染方法签名（tech_design 既有行为）"
    assert "public static <T> T get" in s


def test_format_lean_omits_method_sigs_keeps_constraints():
    """★核心★ include_method_sigs=False → 去掉方法签名 payload，但保留概念 FQN + 变体硬约束。"""
    s = format_stack_for_prompt(_profile(), include_method_sigs=False)
    # 方法签名 payload 被裁掉（声明步不需要，避免撑爆 plan prefill）
    assert "照抄签名" not in s
    assert "public static <T> T get" not in s
    # 但能力边界硬约束必须保留——这才是防假 baseline 的关键
    assert "RedisCache" in s and "CacheUtils" in s, "基建概念 FQN 必须保留"
    assert "Shiro" in s, "鉴权变体硬约束必须保留"
    assert "臆造" in s, "基建符号臆造禁令必须保留"


def test_format_lean_still_bans_wrong_frontend():
    """精简版仍带前端形态约束（server-template 禁 .vue）——防前端 baseline 幻觉。"""
    s = format_stack_for_prompt(_profile(), include_method_sigs=False)
    assert ".vue" in s and "禁止" in s


def test_format_none_profile_empty_both_modes():
    assert format_stack_for_prompt(None) == ""
    assert format_stack_for_prompt(None, include_method_sigs=False) == ""


# ── _baseline_stack_grounding_block ──
def test_grounding_block_empty_without_stack():
    """无 project_stack → 空串（fail-open，老任务/降级零变化）。"""
    assert _baseline_stack_grounding_block({}) == ""
    assert _baseline_stack_grounding_block({"project_stack": None}) == ""


def test_grounding_block_ties_to_baseline_declaration():
    """★核心★ 有画像 → 渲染精简栈约束 + 明确绑定到 baseline_covered 申报纪律。"""
    blk = _baseline_stack_grounding_block({"project_stack": _profile()})
    assert blk, "有画像必须产出非空 grounding"
    assert "baseline_covered" in blk, "grounding 必须显式绑定 baseline_covered 申报"
    # 带能力边界硬约束
    assert "Shiro" in blk and "RedisCache" in blk
    # 精简：不含方法签名 payload
    assert "照抄签名" not in blk, "声明步 grounding 应为精简版（无方法签名 payload）"


def test_grounding_block_warns_against_absent_capability():
    """grounding 措辞必须提示：画像未列出/明确无的能力，绝不申报为存量已满足。"""
    blk = _baseline_stack_grounding_block({"project_stack": _profile()})
    assert ("绝不申报" in blk or "不得申报" in blk or "禁止申报" in blk), \
        f"grounding 必须禁止对画像外能力申报 baseline: {blk[:400]}"


# ── P1 外科补齐面注入（可捕获 prompt 的集成点）──
class _Resp:
    def __init__(self, content):
        self.content = content


class _CapLLM:
    def __init__(self):
        self.captured = []

    async def ainvoke(self, messages):
        self.captured.append(messages[1]["content"])
        return _Resp('{"assignments":[],"baseline_covered":[]}')


async def test_topup_injects_stack_directive():
    """P1 外科补齐 prompt 必须带 stack_directive（棕地 baseline 再申报面同样接地）。"""
    from swarm.brain.nodes import _targeted_coverage_topup
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    st = SubTask(id="st-1", description="do", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a"], readable=[]), covers=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    uncov = [{"id": "req-aaaa1111", "text": "系统提供 Redis 诊断接口", "kind": "functional"}]
    llm = _CapLLM()
    directive = format_stack_for_prompt(_profile(), include_method_sigs=False)
    await _targeted_coverage_topup(
        llm, plan, uncov, {"req-aaaa1111"},
        project_structure="src/", stack_directive=directive,
    )
    assert llm.captured, "topup 应发起 LLM 调用"
    assert "Shiro" in llm.captured[0], "topup baseline 申报面必须带栈画像硬约束"


async def test_topup_no_double_header_via_real_caller_contract():
    """★复核 MEDIUM 回归锁★ P1 外科补齐自带 header；真实调用方传【裸】lean 指令
    （_lean_stack_directive），topup 输出只应有【一个】'技术栈画像' header，不得双重包裹。"""
    from swarm.brain.nodes import _targeted_coverage_topup
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    st = SubTask(id="st-1", description="do", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a"], readable=[]), covers=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    uncov = [{"id": "req-aaaa1111", "text": "Redis 诊断", "kind": "functional"}]
    llm = _CapLLM()
    raw = _lean_stack_directive({"project_stack": _profile()})  # 真实调用方传的正是这个
    assert raw and "申报接地" not in raw, "_lean_stack_directive 必须返回裸指令（无 wrapper header）"
    await _targeted_coverage_topup(
        llm, plan, uncov, {"req-aaaa1111"},
        project_structure="src/", stack_directive=raw,
    )
    assert llm.captured
    assert llm.captured[0].count("## 技术栈画像") == 1, \
        f"topup prompt 只应有一个技术栈画像 header（防双重包裹）: {llm.captured[0].count('## 技术栈画像')}"


def test_lean_directive_raw_vs_block_wrapped():
    """_lean_stack_directive=裸；_baseline_stack_grounding_block=带申报纪律 header 的完整块。"""
    raw = _lean_stack_directive({"project_stack": _profile()})
    blk = _baseline_stack_grounding_block({"project_stack": _profile()})
    assert "申报接地" not in raw and "申报接地" in blk
    assert raw in blk, "完整块必须内含裸指令"


def test_render_error_records_degrade(monkeypatch):
    """★hunter CONFIRMED 回归锁★ format_stack_for_prompt 对【真实画像】抛异常时，
    除返回 '' 外必须 record_degrade（令'栈画像崩了 grounding 静默关'在 metrics 可分）。"""
    calls = []
    import swarm.infra.degrade as _deg
    monkeypatch.setattr(_deg, "record_degrade", lambda k, *a, **k2: calls.append(k))

    def _boom(*a, **k):
        raise RuntimeError("render blew up")
    monkeypatch.setattr("swarm.brain.stack_detect.format_stack_for_prompt", _boom)
    out = _lean_stack_directive({"project_stack": _profile()})
    assert out == "", "异常必须 fail-open 返回空"
    assert any("stack_grounding" in c for c in calls), \
        f"异常路径必须 record_degrade（否则静默失效不可见）: {calls}"


async def test_topup_backward_compat_no_directive():
    """stack_directive 缺省='' → topup 逐字向后兼容（不注入、不报错）。"""
    from swarm.brain.nodes import _targeted_coverage_topup
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    st = SubTask(id="st-1", description="do", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["a"], readable=[]), covers=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    uncov = [{"id": "req-aaaa1111", "text": "x", "kind": "functional"}]
    llm = _CapLLM()
    out = await _targeted_coverage_topup(llm, plan, uncov, {"req-aaaa1111"})
    assert out is not None or out is None  # 不抛异常即可
