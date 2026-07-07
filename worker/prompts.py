"""Worker 系统提示词模板

为 Worker Agent 生成完整的系统提示，包含：
- 角色定义
- 子任务描述 & 验收标准
- 文件 Scope 约束
- 共享接口契约
- 错题集 & 成功范例
"""

from __future__ import annotations

import json
import logging

from swarm.types import FileScope, KnowledgeContext, SubTask

logger = logging.getLogger(__name__)

SYSTEM_PROMPT_TEMPLATE = """\
你是 Swarm Worker Agent — 一个专业的代码实现智能体。

## 🎯 你的任务

### 子任务 ID: {subtask_id}
{subtask_description}

## ✅ 验收标准
{acceptance_criteria}

## 📋 共享接口契约
```json
{contract}
```

## 🔒 文件访问权限（Scope 约束）
- ✏️ 可修改文件（已存在）: {writable_files}
- 📄 需新建文件（已授权，必须创建）: {create_files}
- 👁️ 可读文件: {readable_files}

⚠️ **严格遵守 Scope 约束**：你只能修改"可修改文件"列表中的文件、创建"需新建文件"列表中的文件、读取"可读文件"列表中的文件。超出范围的文件操作将被拒绝。

{stack_section}

## 🔧 验证 Harness（如何确认你的产出合格）
{harness_section}

{coding_standards_section}

{debug_section}

## 🔄 工作流程

### Phase 1: 定位（目标 <5s）
1. 阅读相关文件，理解当前代码结构
2. 定位需要修改的位置
3. 确认接口契约和依赖关系
4. **按需检索**：下方"知识参考"是按相关度预取的精选片段（非全量）。若发现上下文不足
   （不清楚某个工具类/接口/约定的用法），主动调用 `query_knowledge_base` 即时检索本项目
   相关代码，而非凭空猜测或假设——按需取用优于一次灌满。

### Phase 2: 编码（目标 10-60s）
1. 按照契约和验收标准实现修改
2. 使用 patch_file 进行精确编辑
3. 保持代码风格一致

### Phase 3: L1 验证（目标 10-120s）
1. 运行编译（run_compile）确认无语法错误
2. 运行测试（run_tests）确认功能正确
3. 如验证失败，分析原因并修复（最多 {max_fix_rounds} 轮）

### Phase 4: 产出
1. 使用 git_diff 查看你的变更
2. 总结你的修改内容
3. 评估你的置信度（high/medium/low）

## 🚫 限制
- 最多调用 {max_iterations} 次 Tool
- 超时限制 {max_execution_time} 秒
- 不允许修改 Scope 外的文件
- 不允许执行白名单外的命令

## 📝 输出格式
完成所有修改后，在最终回复中包含：
1. **变更摘要**: 简要说明你做了什么
2. **影响范围**: 修改了哪些文件/函数
3. **验证结果**: 编译是否通过、测试是否通过
4. **置信度**: high（完全确信）/ medium（基本确信）/ low（不确定）
5. **注意事项**: 需要人工审查的部分（如有）

{user_profile_section}
{project_knowledge_section}
{knowledge_section}
"""

USER_PROFILE_SECTION_TEMPLATE = """\
## 👤 用户画像（L1 — 实现约束）
{user_profile}
"""

KNOWLEDGE_SECTION_TEMPLATE = """\
## 📚 知识参考

### 🔴 历史错误（请避免）
{mistakes}

### 🟢 成功范例（可参考）
{successes}
"""

# 项目规范 + 相关代码段：让小模型"像熟悉本项目的工程师"那样按既有惯例写代码。
PROJECT_KNOWLEDGE_TEMPLATE = """\
## 🧭 本项目工程经验（务必遵循既有惯例，不要另起炉灶）

### 📐 编码规范 / 约定
{norms}

### 🧩 相关既有代码（优先复用，模仿其风格）
{semantic}
"""


def _cap_section(text: str, max_chars: int) -> str:
    """治本 B：把单段注入内容按字符预算截断（压 prefill）。截断点尽量落在行尾，附可观测标记。"""
    if not text or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    nl = cut.rfind("\n")
    if nl > max_chars * 0.6:  # 尽量整行截断，不切半行
        cut = cut[:nl]
    return cut.rstrip() + f"\n…（上下文预算裁剪：省略 {len(text) - len(cut)} 字以压 prefill / 防流式超时）"


def build_worker_prompt(
    subtask: SubTask,
    scope: FileScope | None = None,
    knowledge: KnowledgeContext | None = None,
    user_profile_prompt: str = "",
    shared_contract: dict | None = None,
    project_stack: dict | None = None,
) -> str:
    """构建 Worker 系统提示词

    Args:
        subtask: 子任务定义
        scope: 文件访问权限，默认使用 subtask.scope
        knowledge: 知识上下文（错题集、成功范例等）

    Returns:
        完整的系统提示词字符串
    """
    from swarm.config.settings import get_config

    effective_scope = scope or subtask.scope
    config = get_config()

    # 验收标准格式化
    if subtask.acceptance_criteria:
        criteria_lines = "\n".join(
            f"  {i}. {c}" for i, c in enumerate(subtask.acceptance_criteria, 1)
        )
    else:
        criteria_lines = "  （无明确验收标准，按子任务描述自行判断）"

    # 契约格式化（Brain shared_contract + 子任务 contract）
    # D51：子任务完整契约改在【派发/构建 prompt 时】合成——shared 打底 + 子任务字段覆盖，
    # 与旧 plan 期 enrich_plan_with_shared_contract 的 merge 语义（dict(shared) 后
    # update(st.contract)）逐字节一致。plan 不再为每个子任务内联一份 shared 副本
    # （50+ 子任务 × ~42K ≈ MB 级 plan，每次 checkpoint 序列化都被展开）。
    # 旧 checkpoint 兼容：恢复出的 subtask.contract 已含 shared（旧 enrich 产物）时，
    # 此处再合成是幂等的（同键同值覆盖），worker 可见契约不变。
    contract_payload: dict = {}
    if shared_contract:
        contract_payload["shared_contract"] = shared_contract
    _merged_contract: dict = dict(shared_contract or {})
    _merged_contract.update(subtask.contract or {})
    if _merged_contract:
        contract_payload["subtask_contract"] = _merged_contract
        # hunter#2 防御性可观测：子任务 contract 覆盖了 shared 同名键且值不同——要么是
        # 合法的子任务级 override（by design 子任务字段优先），要么是旧 checkpoint 里
        # plan 期 enrich 烤进去的【过期 shared 副本】在 replan 更新 shared 后遮蔽新值
        # （无 provenance 无法区分）。记 info 留痕，漂移可排查，不改行为。
        _shadowed = [
            k for k, v in (subtask.contract or {}).items()
            if k in (shared_contract or {}) and (shared_contract or {}).get(k) != v
        ]
        if _shadowed:
            logger.info(
                "[PROMPT] 子任务 %s contract 覆盖 shared_contract 同名键(值不同): %s"
                "（合法 override 或旧 checkpoint 过期 shared 遮蔽，如怀疑后者请核对 replan 历史）",
                getattr(subtask, "id", "?"), sorted(_shadowed)[:10],
            )
    if contract_payload:
        try:
            contract_str = json.dumps(contract_payload, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            contract_str = str(contract_payload)
    else:
        contract_str = "（无契约约束）"

    # Scope 格式化
    writable_files = ", ".join(effective_scope.writable) if effective_scope.writable else "（无）"
    create_files = ", ".join(effective_scope.create_files) if effective_scope.create_files else "（无）"
    readable_files = ", ".join(effective_scope.readable) if effective_scope.readable else "（仅可写/新建文件）"

    # Harness 段落 —— 告诉 Worker 用什么命令验证产出合格
    harness_section = _format_harness_section(getattr(subtask, "harness", None))

    # DEBUG 意图段落 —— 结构化排错 4 阶段
    debug_section = _format_debug_section(subtask)

    # 编码规范段落（L2）—— 按模型档位裁剪量，机器可强制项交给 L0/L1
    from swarm.worker.coding_standards import build_coding_standards_section
    coding_standards_section = build_coding_standards_section(subtask)

    # 知识段落（错题/成功模式）
    knowledge_section = ""
    if knowledge:
        mistakes = knowledge.get("mistakes", [])
        successes = knowledge.get("successes", [])

        if mistakes or successes:
            mistakes_str = _format_mistakes_for_worker(mistakes)
            successes_str = _format_successes_for_worker(successes)
            knowledge_section = KNOWLEDGE_SECTION_TEMPLATE.format(
                mistakes=mistakes_str,
                successes=successes_str,
            )

    # 项目工程经验段落（norms 规范 + semantic 相关代码）——让小模型按本项目惯例写代码。
    # 此前这两类知识虽被检索+传到 worker，却从未注入 prompt（worker 编码时看不到），
    # 是"任务执行蠢笨"的关键原因之一。
    project_knowledge_section = ""
    if knowledge:
        norms = knowledge.get("norms", [])
        semantic = knowledge.get("semantic", [])
        if norms or semantic:
            project_knowledge_section = PROJECT_KNOWLEDGE_TEMPLATE.format(
                norms=_format_norms_for_worker(norms),
                semantic=_format_semantic_for_worker(semantic),
            )

    # 治本 B：按难度给【知识注入】字符预算，压 prefill。并发 + 大上下文(系统提示里的知识/工程经验
    # 是最大且最可裁剪的块)正是本地模型流式首 token 超时的【负载根因】——体量小→prefill 快→不超时
    # 且整体更快。trivial/medium 不需长篇，complex 给足。SWARM_WORKER_CTX_KB_CHARS 调基数(默认 4000)。
    import os as _os
    _kb_base = int(_os.environ.get("SWARM_WORKER_CTX_KB_CHARS", "4000") or 4000)
    _diff = str(getattr(getattr(subtask, "difficulty", ""), "value",
                        getattr(subtask, "difficulty", "")) or "").lower()
    _kb_cap = max(800, int(_kb_base * {"trivial": 0.4, "medium": 1.0, "complex": 2.0}.get(_diff, 1.0)))
    knowledge_section = _cap_section(knowledge_section, _kb_cap)
    project_knowledge_section = _cap_section(project_knowledge_section, _kb_cap)

    # 技术栈权威画像段落（detect_stack 磁盘 ground truth）——把 jakarta/javax 命名空间、
    # Spring Boot/Java 版本等【写对 import 的硬前提】喂到 worker 跟前。此前 project_stack
    # 只到 tech_design/plan，断在 worker 之前 → 本地模型按训练惯性写 javax.* → `package
    # javax.servlet does not exist` → 复读死循环到迭代上限（实测 RuoYi st-3 等 8 子任务）。
    stack_section = ""
    if project_stack:
        try:
            from swarm.brain.stack_detect import format_stack_for_prompt
            _sd = format_stack_for_prompt(project_stack)
            if _sd:
                stack_section = "## 🧱 技术栈权威画像（磁盘 ground truth，优先级最高）\n" + _sd
        except Exception:  # noqa: BLE001
            stack_section = ""

    user_profile_section = ""
    if (user_profile_prompt or "").strip():
        user_profile_section = USER_PROFILE_SECTION_TEMPLATE.format(
            user_profile=user_profile_prompt.strip(),
        )

    # A4(round11)：重试时把 brain 失败诊断作为硬约束块前置到描述，防换模型重试仍重蹈同类错。
    _desc = subtask.description
    _rg = (getattr(subtask, "retry_guidance", "") or "").strip()
    if _rg:
        _desc = (
            "⚠️【上次失败的诊断与硬约束 — 必须遵守，否则重蹈覆辙】\n"
            f"{_rg}\n\n"
            f"{_desc}"
        )

    return SYSTEM_PROMPT_TEMPLATE.format(
        subtask_id=subtask.id,
        subtask_description=_desc,
        acceptance_criteria=criteria_lines,
        contract=contract_str,
        writable_files=writable_files,
        create_files=create_files,
        readable_files=readable_files,
        harness_section=harness_section,
        debug_section=debug_section,
        coding_standards_section=coding_standards_section,
        max_fix_rounds=config.worker.max_fix_rounds,
        max_iterations=config.worker.max_iterations,
        max_execution_time=config.worker.max_execution_time,
        user_profile_section=user_profile_section,
        knowledge_section=knowledge_section,
        project_knowledge_section=project_knowledge_section,
        stack_section=stack_section,
    )


def _format_harness_section(harness) -> str:
    """格式化验证 harness 给 Worker：明确的构建/测试/验收命令。"""
    if harness is None:
        return "（无特定 harness，请用 run_compile/run_tests 做基本验证）"
    lines: list[str] = []
    if getattr(harness, "language", ""):
        lines.append(f"- 语言: {harness.language}")
    if getattr(harness, "setup_commands", None):
        lines.append(f"- 准备: {' && '.join(harness.setup_commands)}")
    if getattr(harness, "build_command", ""):
        lines.append(f"- 构建/语法检查: `{harness.build_command}`（用 run_command 执行）")
    if getattr(harness, "test_command", ""):
        lines.append(f"- 测试: `{harness.test_command}`（用 run_command 执行）")
    if getattr(harness, "verify_commands", None):
        for vc in harness.verify_commands:
            lines.append(f"- 验收: `{vc}`")
    if not lines:
        return "（无特定 harness，请用 run_compile/run_tests 做基本验证）"
    lines.append(
        "完成编码后**必须实际运行上述命令**确认通过，不要仅凭阅读代码就声称验证通过。"
    )
    return "\n".join(lines)


DEBUG_SECTION_TEMPLATE = """\
## 🐛 DEBUG 排错流程（务必严格遵守）

本任务是 DEBUG 意图 — 你必须按以下 **4 阶段** 进行系统性排错，**不许跳过任何阶段**：

### 阶段 A：复现 Bug ⚠️ 必须先做
1. 运行失败用例命令：`{failing_test_command}`
2. **确认该命令当前确实失败**（exit code ≠ 0）— 这是排错的起点
3. 记录完整的错误输出（报错信息、traceback、断言失败细节）
4. **⛔ 未复现就动手改代码 = 禁止。** 你必须亲眼看到 bug 复现，才能进入下一阶段

### 阶段 B：定位根因
1. 根据复现时获得的错误信息，阅读相关代码
2. 追踪调用链，找到 bug 的根本原因（不要凭猜测修改）
3. 确认你理解了 bug 的完整因果链

### 阶段 C：最小修复
1. 只做修复 bug 所需的最小改动，不做额外重构
2. 修复应当直接针对阶段 B 定位的根因
3. 避免过度修改——每多改一行就多一分引入新 bug 的风险

### 阶段 D：回归验证
1. **再次运行失败用例命令**：`{failing_test_command}` — 修复后应**通过**（exit code = 0）
2. 运行完整测试命令确认无回归（如果提供了 test_command）
3. 如果回归测试失败，必须回退到阶段 B 重新定位

**核心原则**：不复现不许改，改完必须验证。
"""


def _format_debug_section(subtask) -> str:
    """当 intent==DEBUG 时，返回结构化排错 4 阶段提示；否则返回空串。"""
    from swarm.types import TaskIntent

    if getattr(subtask, "intent", None) != TaskIntent.DEBUG:
        return ""

    harness = getattr(subtask, "harness", None)
    failing_cmd = getattr(harness, "failing_test_command", "") if harness else ""

    if not failing_cmd:
        # 优雅降级：没有 failing_test_command 也给通用 DEBUG 提示
        return (
            "## 🐛 DEBUG 排错流程（务必严格遵守）\n\n"
            "本任务是 DEBUG 意图 — 你必须按以下 **4 阶段** 进行系统性排错：\n\n"
            "### 阶段 A：复现 Bug ⚠️ 必须先做\n"
            "1. 根据子任务描述中的错误信息，找到并运行可以触发 bug 的命令或测试\n"
            "2. **确认 bug 确实可以复现** — 不复现不许改\n\n"
            "### 阶段 B：定位根因\n"
            "1. 阅读代码，追踪调用链找到根因（不要猜测）\n\n"
            "### 阶段 C：最小修复\n"
            "1. 只做修复所需的最小改动\n\n"
            "### 阶段 D：回归验证\n"
            "1. 再跑一遍阶段 A 的用例——修复后应通过\n"
            "2. 跑完整测试确认无回归\n\n"
            "⚠️ **核心原则**：不复现不许改，改完必须验证。\n"
        )

    return DEBUG_SECTION_TEMPLATE.format(failing_test_command=failing_cmd)


def _format_mistakes_for_worker(items: list[dict]) -> str:
    if not items:
        return "（无）"
    lines = ["⚠️ **历史教训（这类任务曾经犯过的错）**"]
    for i, item in enumerate(items, 1):
        summary = item.get("description") or item.get("title") or f"错题 {i}"
        fix = item.get("fix_description") or item.get("solution") or ""
        # audit #4：metadata 非 dict 时 (… or {}) 已兜底为空 dict，snippet 自然取空串，
        # 原 `if isinstance(...) is False: snippet = ""` 是冗余死逻辑（且 is False 风格怪），移除。
        snippet = (item.get("metadata") or {}).get("code_snippet", "") if isinstance(item.get("metadata"), dict) else ""
        lines.append(f"  {i}. {summary}")
        if fix:
            lines.append(f"     正确做法: {fix[:200]}")
        if snippet:
            lines.append(f"     ```\n{snippet[:400]}\n     ```")
    return "\n".join(lines)


def _format_successes_for_worker(items: list[dict]) -> str:
    if not items:
        return "（无）"
    lines = ["✅ **参考范例（类似任务的成功实现）**"]
    for i, item in enumerate(items, 1):
        summary = item.get("pattern_name") or item.get("description") or f"模式 {i}"
        approach = item.get("approach") or item.get("content") or ""
        snippet = (item.get("metadata") or {}).get("code_snippet", "") if isinstance(item.get("metadata"), dict) else ""
        lines.append(f"  {i}. {summary}")
        if approach and not snippet:
            lines.append(f"     {approach[:200]}")
        if snippet:
            lines.append(f"     ```\n{snippet[:500]}\n     ```")
    return "\n".join(lines)


def _format_norms_for_worker(items: list[dict]) -> str:
    """格式化项目规范（Layer C norms）给 worker：标题 + 内容，按 tag 分组提示。"""
    if not items:
        return "（无项目规范记录）"
    lines: list[str] = []
    for i, item in enumerate(items, 1):
        title = (item.get("title") or item.get("name") or f"规范 {i}").strip()
        content = (item.get("content") or item.get("description") or "").strip()
        tag = (item.get("tag") or "").strip()
        tag_label = f"[{tag}] " if tag and tag not in ("general", "") else ""
        lines.append(f"  {i}. {tag_label}{title}")
        if content:
            # 500（原 300）：norm 内容多为含类名/注解/方法签名的完整约定，300 处常截在句中
            # (实测 RuoYi 规范 10 条 308~393 字被腰斩)。只注入 top-8 norm，+200 字/条 ≈ +500 token，
            # 对 80k 窗口可忽略，却让约定完整可照搬。
            lines.append(f"     {content[:500]}")
    return "\n".join(lines)


def _format_semantic_for_worker(items: list[dict]) -> str:
    """格式化相关既有代码片段（Layer B semantic）给 worker：路径 + 符号 + 代码摘要。

    让小模型"看到"项目里相关的现成实现，按其风格写、优先复用工具类，而非凭空造。
    """
    if not items:
        return "（无相关代码片段）"
    lines: list[str] = []
    for i, item in enumerate(items, 1):
        fp = (item.get("file_path") or item.get("path") or "").strip()
        sym = (item.get("symbol") or item.get("name") or "").strip()
        snippet = (item.get("content") or item.get("text") or item.get("code") or "").strip()
        header = fp or sym or f"片段 {i}"
        if fp and sym:
            header = f"{fp} :: {sym}"
        lines.append(f"  {i}. `{header}`")
        if snippet:
            lines.append(f"     ```\n{snippet[:600]}\n     ```")
    return "\n".join(lines)



