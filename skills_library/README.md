# 经验拔插层 · 技能库（skills_library）

放一个 `.md` 即多一个技能，**零代码改动**。本目录是内置种子库；用户可另挂导入目录
（`SWARM_SKILLS_DIR=skills_library,/path/to/your/skills`，靠前优先、可覆盖内置）。

本层是 **advisory 知识注入**（"怎么做更好"），永不阻断交付——与固化闸（T1-T4/L1/L2/
覆盖闸）互不相干。不依赖任何 CLI / Claude Code / MCP，纯文件解析。

## 支持两种 drop-in 形态（同目录可混放）

### 1. native（本层原生，精确路由）
扁平 `*.md`，frontmatter 显式声明路由标签：

```markdown
---
id: my-skill                 # 唯一 slug（缺省取文件名/目录名）
title: 展示名
applies_to_stacks: ["*"]     # "*" 或语言集，如 ["python","java"]
applies_to_intents: ["create","modify"]   # create|modify|debug|audit|refactor|"*"
applies_to_phases: ["code","produce"]      # plan|code|produce|"*"
target: ["worker","planner"] # 注入到哪个提示面
priority: 55                 # 0-100，降序排
max_chars: 1000              # 本技能注入上限
tags: ["idiom"]
---
<正文：简洁、可执行的经验条目（bullet 优先）。绝不写死示例项目/表名。>
```

### 2. imported（第三方，如 ECC / Claude Code / 自有技能包）
标准 `<name>/SKILL.md` 布局，frontmatter 只有 `name` + `description` 也能直接被消费——
路由字段全缺时落宽默认（stacks/intents/phases=`*`，target=worker），零编辑即可导入。
建议用大模型按需精炼（见 docs/PLUGGABLE_EXPERIENCE_LAYER_HANDOFF.md 附录 A2）。

## push vs pull（两个注入面不同机制）
- **worker = pull（离散工具）**：选择器按 `栈×意图×阶段` 收窄候选（封顶 `worker_max_tools`，
  默认 15），每条挂成一个独立工具 `experience__<id>`，**由小模型自己决定调哪个**（或不调）。
  提示词只挂一份轻量目录（标题+摘要），正文调用工具才拉——省 prefill、给足选择空间。
- **planner = push（全文注入）**：planner 是单发无工具调用，按 `栈×plan` 选中 top-`max_k`
  直接把正文拼进提示。

`栈×意图×阶段` 标签预筛 → 优先级降序 → 封顶 → 可选 LLM rerank（`SWARM_SKILLS_RERANK=1`，默认关）。
配置见 `SWARM_SKILLS_*`（config/settings.py: SkillsConfig）。

## 铁律
栈特化只进正文 + `applies_to_stacks` 标签；任一异常 fail-open 到空串；不重复固化闸
（密钥扫描/验证/TDD 闸已确定性执行，别做成技能）。
