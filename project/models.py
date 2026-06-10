"""Project/Task 数据模型 — Pydantic v2 模型定义

对应 PG 表: projects / task_records / preprocess_progress
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 项目状态
# ──────────────────────────────────────────────
class ProjectStatus(str, Enum):
    """项目生命周期状态"""
    EMPTY = "EMPTY"                  # 刚创建，尚未预处理
    PREPROCESSING = "PREPROCESSING"  # 正在预处理
    READY = "READY"                  # 预处理完成，可接任务
    ERROR = "ERROR"                  # 预处理失败


class GraphStatus(str, Enum):
    """CodeGraph 索引状态"""
    NONE = "NONE"        # 未索引
    INDEXING = "INDEXING"  # 正在索引
    INDEXED = "INDEXED"   # 索引完成
    ERROR = "ERROR"       # 索引失败


# ──────────────────────────────────────────────
# 预处理阶段
# ──────────────────────────────────────────────
class PreprocessPhase(str, Enum):
    """预处理管道阶段"""
    IDLE = "idle"
    SCANNING = "scanning"      # 扫描文件结构
    INDEXING = "indexing"      # codegraph index
    EMBEDDING = "embedding"    # 嵌入向量到 Qdrant
    ANALYZING = "analyzing"    # LLM 生成项目摘要 / 架构图
    COMPLETE = "complete"
    ERROR = "error"


# ──────────────────────────────────────────────
# Project 模型
# ──────────────────────────────────────────────
class Project(BaseModel):
    """项目模型 — 对应 PG projects 表"""
    id: str = Field(description="UUID 主键")
    name: str = Field(description="项目名称")
    path: str = Field(description="项目根目录绝对路径")
    description: str = Field(default="", description="项目描述")
    status: ProjectStatus = Field(default=ProjectStatus.EMPTY, description="项目状态")
    graph_status: GraphStatus = Field(default=GraphStatus.NONE, description="CodeGraph 索引状态")
    graph_progress: float = Field(default=0.0, description="索引进度 0.0~1.0")
    graph_error: str | None = Field(default=None, description="索引错误信息")
    file_count: int = Field(default=0, description="文件数量")
    symbol_count: int = Field(default=0, description="符号数量")
    language_breakdown: dict[str, int] = Field(
        default_factory=dict,
        description="语言分布 {\"Python\": 120, \"TypeScript\": 45}",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="项目级配置（模型偏好、沙箱模板等）",
    )
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


# ──────────────────────────────────────────────
# PreprocessProgress 模型
# ──────────────────────────────────────────────
class PreprocessProgress(BaseModel):
    """预处理进度 — 对应 PG preprocess_progress 表"""
    project_id: str = Field(description="关联项目 ID")
    phase: PreprocessPhase = Field(default=PreprocessPhase.IDLE, description="当前阶段")
    phase_progress: float = Field(default=0.0, description="当前阶段进度 0.0~1.0")
    message: str = Field(default="", description="进度描述消息")
    started_at: datetime | None = Field(default=None, description="预处理开始时间")
    completed_at: datetime | None = Field(default=None, description="预处理完成时间")
    error: str | None = Field(default=None, description="错误信息")
    # 分阶段统计
    scan_stats: dict[str, Any] = Field(
        default_factory=dict,
        description="扫描统计 {files: N, dirs: N, languages: [...], line_counts: {...}}",
    )
    index_stats: dict[str, Any] = Field(
        default_factory=dict,
        description="索引统计 {symbols: N, edges: N, time_ms: N}",
    )
    embed_stats: dict[str, Any] = Field(
        default_factory=dict,
        description="嵌入统计 {vectors: N, dim: N}",
    )
    analysis_stats: dict[str, Any] = Field(
        default_factory=dict,
        description="分析统计 {summary_tokens: N, entities: N}",
    )


# ──────────────────────────────────────────────
# TaskRecord 模型
# ──────────────────────────────────────────────
class TaskRecord(BaseModel):
    """任务记录 — 对应 PG task_records 表

    关联 BrainState: task_record.id == brain_state.task_id
    """
    id: str = Field(description="任务 ID = BrainState.task_id")
    project_id: str = Field(description="关联项目 ID")
    description: str = Field(description="原始任务描述 = BrainState.task_description")
    status: str = Field(default="SUBMITTED", description="任务状态 (TaskStatus 枚举值)")
    complexity: str | None = Field(default=None, description="LLM 判定复杂度 (Complexity 枚举值)")
    plan: dict[str, Any] | None = Field(default=None, description="序列化的 TaskPlan")
    subtask_count: int = Field(default=0, description="子任务总数")
    completed_subtasks: int = Field(default=0, description="已完成子任务数")
    human_decision: str | None = Field(default=None, description="人工决策 (HumanDecision 枚举值)")
    merged_diff: str | None = Field(default=None, description="合并后的 diff")
    thread_id: str | None = Field(default=None, description="LangGraph checkpointer thread_id")
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
