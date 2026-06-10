"""记忆层 L0–L6 与验证层 V1–V3 命名对照（避免歧义）。"""

from __future__ import annotations

# 记忆层（MemoryLayer in types.py）
MEMORY_L0_SESSION = "L0"       # 会话元数据，ephemeral
MEMORY_L1_PROFILE = "L1"       # 用户结构化档案
MEMORY_L2_TASK_DIGEST = "L2"   # 近期任务摘要
MEMORY_L3_SLIDING = "L3"       # LangGraph 滑动窗口
MEMORY_L4_KNOWLEDGE = "L4"     # 知识库 A-D
MEMORY_L5_MISTAKES = "L5"      # 错题集
MEMORY_L6_SUCCESSES = "L6"     # 成功模式集

# 验证层（与记忆 L 编号无关）
VERIFY_V1_WORKER = "V1"        # Worker: scope + compile + scoped test
VERIFY_V2_INTEGRATION = "V2" # merge 后 integration_review / verify_l2
VERIFY_V3_STAGING = "V3"       # GitLab CI / staging / verify_l3
