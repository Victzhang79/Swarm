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

from swarm.types import FileScope, KnowledgeContext, SubTask

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
- ✏️ 可写文件: {writable_files}
- 👁️ 可读文件: {readable_files}

⚠️ **严格遵守 Scope 约束**：你只能修改可写文件，只能读取可读文件。超出范围的文件操作将被拒绝。

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


def build_worker_prompt(
    subtask: SubTask,
    scope: FileScope | None = None,
    knowledge: KnowledgeContext | None = None,
    user_profile_prompt: str = "",
    shared_contract: dict | None = None,
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
    contract_payload: dict = {}
    if shared_contract:
        contract_payload["shared_contract"] = shared_contract
    if subtask.contract:
        contract_payload["subtask_contract"] = subtask.contract
    if contract_payload:
        try:
            contract_str = json.dumps(contract_payload, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            contract_str = str(contract_payload)
    else:
        contract_str = "（无契约约束）"

    # Scope 格式化
    writable_files = ", ".join(effective_scope.writable) if effective_scope.writable else "（无）"
    readable_files = ", ".join(effective_scope.readable) if effective_scope.readable else "（仅可写文件）"

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

    user_profile_section = ""
    if (user_profile_prompt or "").strip():
        user_profile_section = USER_PROFILE_SECTION_TEMPLATE.format(
            user_profile=user_profile_prompt.strip(),
        )

    return SYSTEM_PROMPT_TEMPLATE.format(
        subtask_id=subtask.id,
        subtask_description=subtask.description,
        acceptance_criteria=criteria_lines,
        contract=contract_str,
        writable_files=writable_files,
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
            lines.append(f"     {content[:300]}")
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



