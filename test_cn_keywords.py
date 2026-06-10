#!/usr/bin/env python3
"""验证中文关键词提取"""
from swarm.knowledge.retriever import _extract_keywords, _apply_time_decay

# 问题1: 中文关键词
print("=== 中文关键词提取 ===")
print("纯中文:", _extract_keywords("修改用户登录模块的密码验证逻辑"))
print("英文:", _extract_keywords("fix Main class bug"))
print("混合:", _extract_keywords("修改 UserService 的密码验证逻辑"))
print("停用词:", _extract_keywords("的实现和修改"))

# 问题2: 时间衰减
print("\n=== 时间衰减 ===")
scores = {"a.py": 2.0, "b.py": 1.5, "c.py": 1.0}
# 最近修改的是 c.py
times = {"a.py": 1000.0, "b.py": 2000.0, "c.py": 3000.0}
result = _apply_time_decay(scores, times)
print("原始:", scores)
print("时间加权后:", result)
# c.py 应该得分最高 (最近修改)
print("c.py 获得了时间加成:", result["c.py"] > 1.0)

# 空时间字典
result2 = _apply_time_decay({"a.py": 2.0}, {})
print("空时间字典(优雅降级):", result2)
