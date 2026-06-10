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

## 🔄 工作流程

### Phase 1: 定位（目标 <5s）
1. 阅读相关文件，理解当前代码结构
2. 定位需要修改的位置
3. 确认接口契约和依赖关系

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

    # 知识段落
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
        max_fix_rounds=config.worker.max_fix_rounds,
        max_iterations=config.worker.max_iterations,
        max_execution_time=config.worker.max_execution_time,
        user_profile_section=user_profile_section,
        knowledge_section=knowledge_section,
    )


def _format_mistakes_for_worker(items: list[dict]) -> str:
    if not items:
        return "（无）"
    lines = ["⚠️ **历史教训（这类任务曾经犯过的错）**"]
    for i, item in enumerate(items, 1):
        summary = item.get("description") or item.get("title") or f"错题 {i}"
        fix = item.get("fix_description") or item.get("solution") or ""
        snippet = (item.get("metadata") or {}).get("code_snippet", "")
        if isinstance(item.get("metadata"), dict) is False:
            snippet = ""
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



