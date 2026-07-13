#!/usr/bin/env python3
"""Swarm — 蜂群 AI 编程智能体系统

一个基于 LangGraph 状态机 + LangChain Agent 的企业级 AI 编程助手。
"""

# 加载 .env
from dotenv import load_dotenv

load_dotenv()

from swarm.config import get_config

# 发版必须与 pyproject.toml:version 同步 bump（round42 教训：双源漂移使 /api/health
# 报旧版→e2e 版本一致性闸门误判"没加载新代码"）。CI 有 test_version_sync 对账。
__version__ = "0.9.44"

# get_config 作为包级公共 API 显式 re-export（保留意图，避免 F401 误删）
__all__ = ["get_config", "__version__"]
