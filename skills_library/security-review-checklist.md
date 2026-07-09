---
id: security-review-checklist
title: 安全自查清单（栈无关·清单形态）
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code", "produce"]
target: ["worker"]
priority: 60
max_chars: 1000
tags: ["security", "checklist"]
---
写涉及输入/鉴权/存储/外呼的代码时，逐条自查（这是提质清单，非交付闸；密钥硬编码另有确定性扫描阻断）：
- 注入：所有外部输入进 SQL/命令/模板/路径前参数化或白名单校验，绝不字符串拼接。
- 鉴权/越权：每个受保护入口显式校验身份与权限；不靠前端隐藏当鉴权；对象级权限逐次校验（防 IDOR）。
- 密钥：不硬编码密钥/密码/token；走环境变量或密钥库；日志/报错不回显敏感值。
- 输出编码：回显到 HTML/JSON/header 前按目标上下文编码，防 XSS/头注入。
- SSRF/路径穿越：外部可控的 URL/文件路径先归一化并限制到允许范围。
- 加密：用标准库算法，不自造 crypto；随机数用密码学安全源。
- 依赖：新增依赖优先成熟库、避免已知漏洞版本。
