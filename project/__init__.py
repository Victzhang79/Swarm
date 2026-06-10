"""Swarm 项目管理模块 — Project/Task 数据模型 + PG 持久化 + 预处理管道

子模块:
- models:  Project / PreprocessProgress / TaskRecord Pydantic 模型
- store:   PostgreSQL 建表 + CRUD
- preprocess: 四阶段预处理管道 (scan → index → embed → analyze)
- codegraph:  CodeGraph CLI 封装
"""

from swarm.project.models import (
    GraphStatus,
    PreprocessPhase,
    PreprocessProgress,
    Project,
    ProjectStatus,
    TaskRecord,
)

__all__ = [
    "GraphStatus",
    "PreprocessPhase",
    "PreprocessProgress",
    "Project",
    "ProjectStatus",
    "TaskRecord",
]
