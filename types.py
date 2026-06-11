"""Swarm 核心类型定义 — 全局共享的数据模型"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypedDict

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 任务复杂度
# ──────────────────────────────────────────────
class Complexity(str, Enum):
    SIMPLE = "simple"       # 改配置/加字段 → 单 Worker
    MEDIUM = "medium"       # 单模块功能 → 2-3 Worker 串行
    COMPLEX = "complex"     # 跨模块 Feature → 多 Worker 并行
    ULTRA = "ultra"         # 架构变更 → 先出方案让人确认


# ──────────────────────────────────────────────
# 任务状态（LangGraph 状态机节点）
# ──────────────────────────────────────────────
class TaskStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    VALIDATING_PLAN = "VALIDATING_PLAN"
    CONFIRMING = "CONFIRMING"          # 等人工确认
    DISPATCHING = "DISPATCHING"
    MONITORING = "MONITORING"
    HANDLING_FAILURE = "HANDLING_FAILURE"
    MERGING = "MERGING"
    VERIFYING_L2 = "VERIFYING_L2"
    DELIVERING = "DELIVERING"
    IN_REVISION = "IN_REVISION"
    LEARNING_SUCCESS = "LEARNING_SUCCESS"
    LEARNING_FAILURE = "LEARNING_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    DONE = "DONE"


# ──────────────────────────────────────────────
# 人工决策
# ──────────────────────────────────────────────
class HumanDecision(str, Enum):
    ACCEPT = "accept"
    REVISE = "revise"
    REJECT = "reject"


# ──────────────────────────────────────────────
# 文件 Scope（Worker 权限控制）
# ──────────────────────────────────────────────
class FileScope(BaseModel):
    """定义 Worker 对文件的访问权限 + 文件操作意图。

    操作语义（解决"只有改、没有增删"的缺陷）：
    - writable:     现有文件，允许【修改】（patch/write）。
    - create_files: 新文件，需要【新建】（worker 不应先读取，直接 write）。
    - delete_files: 需要【删除】的现有文件。
    - readable:     只读上下文（不修改）。
    writable/create_files/delete_files 三者共同构成"可写权限"，scope_guard 据此放行。
    """
    writable: list[str] = Field(default_factory=list, description="可修改的现有文件")
    readable: list[str] = Field(default_factory=list, description="只读上下文文件")
    create_files: list[str] = Field(default_factory=list, description="需新建的文件")
    delete_files: list[str] = Field(default_factory=list, description="需删除的文件")

    def is_writable(self, path: str) -> bool:
        targets = self.writable + self.create_files + self.delete_files
        return any(path.endswith(p) or p.endswith(path) for p in targets)

    def is_readable(self, path: str) -> bool:
        return self.is_writable(path) or any(
            path.endswith(p) or p.endswith(path) for p in self.readable
        )

    def is_create(self, path: str) -> bool:
        return any(path.endswith(p) or p.endswith(path) for p in self.create_files)

    def is_delete(self, path: str) -> bool:
        return any(path.endswith(p) or p.endswith(path) for p in self.delete_files)

    def all_write_targets(self) -> list[str]:
        """所有写目标（修改+新建+删除），去重保序。"""
        out: list[str] = []
        for f in self.writable + self.create_files + self.delete_files:
            if f and f not in out:
                out.append(f)
        return out


# ──────────────────────────────────────────────
# 子任务定义（Brain 拆解后的产物）
# ──────────────────────────────────────────────
class SubTaskDifficulty(str, Enum):
    """子任务执行难度"""
    TRIVIAL = "trivial"    # 改CSS/修typo/加日志/加注释/简单配置变更
    MEDIUM = "medium"      # 加API端点/修中等bug/加页面/加测试/单模块功能
    COMPLEX = "complex"    # 架构重构/跨模块变更/安全相关/性能优化/复杂算法


class SubTaskModality(str, Enum):
    """子任务输入模态"""
    TEXT = "text"              # 纯文本任务
    MULTIMODAL = "multimodal"  # 需要看图/UI截图/设计图/文档图片


class SubTask(BaseModel):
    """一个可独立执行的子任务"""
    id: str
    description: str
    difficulty: SubTaskDifficulty = SubTaskDifficulty.MEDIUM
    modality: SubTaskModality = SubTaskModality.TEXT
    scope: FileScope
    contract: dict[str, Any] = Field(default_factory=dict, description="共享接口契约")
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, description="依赖的子任务 ID")
    model_preference: str | None = None


# ──────────────────────────────────────────────
# 子任务 DAG（执行计划）
# ──────────────────────────────────────────────
class TaskPlan(BaseModel):
    """Brain 生成的执行计划 — 子任务 DAG"""
    subtasks: list[SubTask]
    parallel_groups: list[list[str]] = Field(
        default_factory=list,
        description="可并行执行的子任务组（每组内的子任务无依赖关系）",
    )
    shared_contract: dict[str, Any] = Field(
        default_factory=dict,
        description="Brain 统一定义的跨子任务共享接口契约",
    )

    def get_ready_tasks(self, completed_ids: set[str]) -> list[SubTask]:
        """获取当前可执行的子任务（依赖已全部完成）"""
        return [
            t for t in self.subtasks
            if t.id not in completed_ids and all(d in completed_ids for d in t.depends_on)
        ]

    def get_dispatch_batch(
        self,
        completed_ids: set[str],
        dispatch_remaining: list[str],
        max_concurrent: int,
    ) -> list[SubTask]:
        """按 parallel_groups 选取下一批可派发子任务。

        同一组内可并行；组间顺序执行。若 parallel_groups 为空则回退到
        get_ready_tasks + max_concurrent 截断。
        """
        remaining = set(dispatch_remaining)
        if not remaining:
            return []

        subtask_by_id = {t.id: t for t in self.subtasks}

        def _is_ready(task: SubTask) -> bool:
            return task.id not in completed_ids and all(
                d in completed_ids for d in task.depends_on
            )

        if self.parallel_groups:
            for group in self.parallel_groups:
                group_remaining = [tid for tid in group if tid in remaining]
                if not group_remaining:
                    continue
                ready_in_group = [
                    subtask_by_id[tid]
                    for tid in group_remaining
                    if tid in subtask_by_id and _is_ready(subtask_by_id[tid])
                ]
                if ready_in_group:
                    return ready_in_group[:max_concurrent]
                return []

        ready = [
            t for t in self.get_ready_tasks(completed_ids) if t.id in remaining
        ]
        return ready[:max_concurrent]

    def all_completed(self, completed_ids: set[str]) -> bool:
        return all(t.id in completed_ids for t in self.subtasks)


# ──────────────────────────────────────────────
# Worker 产出
# ──────────────────────────────────────────────
class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class WorkerOutput(BaseModel):
    """Worker 执行完子任务后的产出"""
    subtask_id: str
    diff: str = Field(description="git diff 格式的变更")
    summary: str = Field(description="变更说明")
    confidence: Confidence = Confidence.MEDIUM
    l1_passed: bool = False
    l1_details: dict[str, Any] = Field(default_factory=dict)
    execution_log: str = ""
    notes: str = Field(default="", description="需人工审查的部分（Worker 自报，供审批/学习节点参考）")


# ──────────────────────────────────────────────
# 知识检索结果
# ──────────────────────────────────────────────
class KnowledgeContext(TypedDict, total=False):
    """Brain 检索到的知识上下文"""
    struct: list[dict]       # Layer A: 结构索引
    semantic: list[dict]     # Layer B: 语义检索
    norms: list[dict]        # Layer C: 项目规范
    behavior: list[dict]     # Layer D: 历史行为
    mistakes: list[dict]     # L5: 错题集
    successes: list[dict]    # L6: 成功模式集
    project_summary: str     # 预处理 ANALYZE 生成的项目摘要
    preprocess_stats: dict   # 预处理各阶段统计
    affected_files: list[str]       # Layer A 定位 + 依赖扩展的文件集
    hybrid_ranked_files: list[str]     # A+B 融合排序文件
    hybrid_scores: dict[str, float]  # 融合分数


# ──────────────────────────────────────────────
# 记忆层级
# ──────────────────────────────────────────────
class MemoryLayer(str, Enum):
    L0_SESSION = "L0"        # 内存，用完即弃
    L1_USER_PROFILE = "L1"   # PostgreSQL JSON
    L2_TASK_SUMMARY = "L2"   # PostgreSQL 滚动 50 条
    L3_SLIDING_WINDOW = "L3" # LangGraph State
    L4_KNOWLEDGE = "L4"      # Qdrant + PG
    L5_MISTAKES = "L5"       # PG + 向量
    L6_SUCCESSES = "L6"      # PG + 向量
