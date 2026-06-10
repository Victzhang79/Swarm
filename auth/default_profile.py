"""默认 L1 用户画像 — 编排时注入 Brain / Worker LLM prompt。"""

from __future__ import annotations

from typing import Any

GLOBAL_PROFILE_SUFFIX = "__global__"

DEFAULT_ADMIN_PROFILE: dict[str, Any] = {
    "version": 1,
    "identity": {
        "display_name": "Administrator",
        "role": "tech_lead",
    },
    "instructions_for_brain": [
        "拆解子任务时优先「最小可行改动」，禁止无关重构或 scope creep",
        "每个子任务的 acceptance_criteria 必须可自动验证（编译/测试/类型检查至少一项）",
        "并行组内子任务不得写同一文件；有依赖时用 depends_on 串行",
        "complex 任务优先拆成可独立验证的小步，避免单个 Worker 承担跨模块大改",
        "计划验证时对照用户 quality_bar：逻辑变更需测试、禁止提交密钥",
    ],
    "instructions_for_worker": [
        "用中文解释思路与摘要；代码、标识符、commit message 主体用英文",
        "严格遵循 Scope，只改 writable 文件；风格与周边代码保持一致",
        "优先最小 diff：能改一行不写十行，能局部修不整文件重写",
        "逻辑变更后运行 compile/test；失败则小步修复，最多 3 轮",
        "产出前 git_diff 自检，summary 说明影响范围与验证结果",
    ],
    "preferences": {
        "language": "zh-CN",
        "response_language": "中文说明 + 英文代码",
        "coding_style": "简洁、可维护、与仓库现有风格一致",
        "comment_density": "minimal",
        "diff_scope": "最小必要改动",
        "test_framework": "pytest",
        "commit_message_style": "conventional commits，中文描述意图",
    },
    "tech_stack": {
        "backend": ["Python 3.11+", "FastAPI", "LangGraph"],
        "frontend": ["Vanilla JS", "SSE"],
        "database": ["PostgreSQL", "pgvector", "Qdrant"],
        "infra": ["E2B Sandbox", "Docker Compose"],
    },
    "workflow": {
        "review_before_apply": True,
        "prefer_incremental_changes": True,
        "parallel_subtasks": True,
        "on_merge_conflict": "先 3-way 自动消解，失败则标记人工处理",
        "on_test_failure": "定位根因后小步修复，最多 3 轮",
    },
    "quality_bar": {
        "require_tests_for_logic_changes": True,
        "lint_before_commit": True,
        "no_secrets_in_code": True,
    },
    "notes": (
        "L1 用户画像会在 Brain 编排（analyze/plan/validate）与 Worker 执行时注入 LLM prompt。"
        "Web UI 保存后写入「当前用户 + 当前项目」；未配置时回退到用户全局画像。"
    ),
}
