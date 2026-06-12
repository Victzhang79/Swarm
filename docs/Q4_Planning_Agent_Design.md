# Q4 设计：独立规划 Agent（渐进明细 + ≤3 澄清 + LangSmith 上报）

> 状态：设计稿 v1 · 待评审 · 作者：CTO 协作
> 范围：在现有 `brain/` LangGraph 状态机上**增量增强**，不另起炉灶、不重写既有节点。

---

## 0. 一句话目标

让 Brain 在 `ANALYZE → PLAN` 之间具备**主动澄清**能力：当任务描述信息不足以做出可靠规划时，
向人类提出**最多 3 个**高价值问题；规划过程采用**渐进明细**（先粗后细，分层展开），
全过程关键决策点以**结构化 feedback 上报 LangSmith**，便于离线复盘规划质量。

---

## 1. 现状与缺口（地面真相，已核对代码）

| 能力 | 现状 | 文件 |
|---|---|---|
| 状态机骨架 | `analyze→plan→validate_plan→[confirm(ultra)]→dispatch...` | `brain/graph.py` |
| 人工中断 | `confirm_plan` 已用 `interrupt()`，PostgresSaver + 跨 HTTP resume **已跑通** | `brain/nodes.py:804` |
| auto_accept 旁路 | API 模式 `state["auto_accept"]` / `SWARM_AUTO_ACCEPT` 跳过 interrupt | `brain/nodes.py:816` |
| LangSmith 上报 | `push_l1_feedback`（结构化 feedback）、`swarm_traceable`、`brain_graph_config` 已具备 | `tracing.py` |
| 复杂度判定 | 启发式 + LLM 双路，输出 `complexity` | `brain/nodes.py:447` |

**缺口（本设计要补的）**：
1. 没有 `clarify` 节点 —— 任务模糊时直接硬规划，靠 `validate_plan` 重试 3 次兜底，浪费且质量差。
2. `plan` 是一次性出全量 DAG —— 没有"渐进明细"（先出里程碑/模块骨架，再细化子任务）。
3. 规划阶段没有专门的 LangSmith feedback —— 只有 L1 验证阶段有。规划好坏无法离线量化。

---

## 2. 设计决策（含取舍）

### 2.1 澄清问题：策略与上限

**触发条件（必须同时满足，避免过度打扰）**：
- `complexity ∈ {complex, ultra}`（simple/medium 不澄清，直接规划——降低交互摩擦）
- 且 `analyze` 阶段产出 `ambiguity_score ≥ 阈值`（见下）
- 且 `auto_accept == False`（API/CI 自动化模式永不澄清，走默认假设）

**ambiguity_score 怎么来**：复用 `analyze` 的 LLM 调用，在其 JSON 输出里**增加**字段（加法，不改现有 schema 的已用字段）：
```json
{
  "complexity": "...",          // 现有
  "ambiguity_score": 0.0-1.0,   // 新增：信息缺口程度
  "clarifying_questions": [     // 新增：最多 3 条，按价值降序
    {"q": "...", "why": "为何这个问题影响规划", "default_if_skipped": "用户跳过时的默认假设"}
  ]
}
```
- 阈值默认 `0.5`，配置项 `KnowledgeConfig.clarify_threshold`（opt-in 可调，默认开但阈值保守）。
- **硬上限 3 条**：在节点里 `questions[:3]` 截断，无论 LLM 吐多少。理由：>3 个问题人类不耐烦，且单次澄清应聚焦最关键缺口；剩余不确定性留给 `validate_plan` 重试 + 人工 confirm 兜底。
- 每个问题**必带 `default_if_skipped`**：人类可以整体跳过（resume `{"action":"skip"}`），此时用默认假设继续，规划不阻塞。

**为什么不做多轮澄清**：一轮（≤3 问）是成本/收益最优点。多轮会让简单任务也陷入对话泥潭，违背"独立规划 agent"的自动化初衷。多出的不确定性由"渐进明细"的粗粒度先行 + 后续 confirm 消化。

### 2.2 渐进明细（Progressive Elaboration）

把单次 `plan` 拆成**两层**，而非一次性出全量 DAG：

- **L0 骨架（milestone/module 级）**：`plan` 节点先产出 3-7 个粗粒度里程碑（每个含目标 + 涉及模块 + 风险），不含可执行子任务。
- **L1 明细（subtask 级）**：`elaborate` 节点对每个里程碑展开为可派发子任务 DAG（即现有 `TaskPlan` 结构）。

**取舍**：
- 对 `simple/medium`：跳过 L0，直接走现有一次性 `_build_simple_plan`（零回归、零额外 LLM 成本）。
- 对 `complex/ultra`：走两层。理由——大任务一次性出全量 DAG 容易"幻觉式过度拆分"（RuoYi e2e 已暴露 88→1 scope 膨胀），分层让每层 LLM 上下文更聚焦。
- 渐进明细的中间产物（骨架）也是一个**天然的人工 confirm 点**（ultra 任务在骨架层就能拦下方向性错误，比在全量 DAG 后拦更省）。

### 2.3 LangSmith 上报

复用 `push_l1_feedback` 的模式，新增 `push_planning_feedback(...)`，在 `clarify` 和 `elaborate` 后上报：
- `clarify_triggered`（bool）、`clarify_question_count`（0-3）、`ambiguity_score`（0-1）
- `clarify_answered` vs `clarify_skipped`（人类是否真答了）
- `plan_milestone_count`、`plan_subtask_count`、`plan_elaboration_ratio`（subtask/milestone，衡量拆分密度）
- tags 复用 `_base_tags(phase=PHASE_1, component="brain", extra=["planning"])`
- **tracing 关闭时全程 no-op**（与现有约定一致，可观测不影响主流程）

---

## 3. 状态机改动（graph.py）

```
ANALYZE
  └─(after_analyze)─→ CLARIFY        [complex/ultra & ambiguity≥阈值 & !auto_accept]
  └────────────────→ PLAN            [其余：simple/medium 或 低歧义 或 auto_accept]

CLARIFY ─(interrupt 等人类答/skip)─→ PLAN

PLAN (产出 L0 骨架 for complex/ultra；simple/medium 直接全量)
  └─(after_plan)─→ ELABORATE         [complex/ultra：骨架→子任务 DAG]
  └──────────────→ VALIDATE_PLAN     [simple/medium：已是全量，直接校验]

ELABORATE ─→ VALIDATE_PLAN
（VALIDATE_PLAN 之后维持现状：confirm(ultra)/plan(retry)/dispatch）
```

**新增节点**：`clarify`（interrupt）、`elaborate`（LLM 细化）。
**新增条件边**：`after_analyze`、`after_plan`。
**改动现有**：`analyze` 的 LLM 输出 schema 加 2 字段；graph 入口边 `analyze→plan` 改为条件边。
**零改动**：dispatch 及之后的整条链路不动。

---

## 4. BrainState 新增字段（state.py，纯加法）

```python
# ─── 澄清阶段（Q4 规划增强）───
ambiguity_score: float               # analyze 产出的信息缺口评分 0-1
clarifying_questions: list[dict]     # [{q, why, default_if_skipped}]，≤3
clarification_answers: dict          # 人类答复 {q_index: answer} 或 {"action":"skip"}
clarify_skipped: bool                # 人类整体跳过，用默认假设

# ─── 渐进明细 ───
plan_milestones: list[dict]          # L0 骨架：[{goal, modules, risks}]
plan_elaborated: bool                # 是否已从骨架展开为子任务 DAG
```

---

## 5. clarify 节点骨架（复用 confirm_plan 的 interrupt 模板）

```python
def clarify(state: BrainState) -> dict:
    questions = (state.get("clarifying_questions") or [])[:3]   # 硬上限
    if not questions:
        return {"clarify_skipped": True}
    if state.get("auto_accept") or os.environ.get("SWARM_AUTO_ACCEPT","").lower() in ("1","true","yes"):
        return {"clarify_skipped": True}                        # 自动化模式：默认假设
    answer = interrupt({
        "type": "clarify",
        "task_id": state.get("task_id"),
        "task_description": state.get("task_description"),
        "questions": questions,
        "message": "规划前需要澄清以下关键点（可逐条回答，也可整体跳过用默认假设）。",
    })
    if isinstance(answer, dict) and answer.get("action") == "skip":
        return {"clarify_skipped": True, "clarification_answers": {}}
    return {"clarification_answers": answer if isinstance(answer, dict) else {}}
    # plan 节点读 clarification_answers 注入 LLM 上下文
```

---

## 6. 验证策略（分级，对齐用户偏好）

| 改动 | 验证方式 | 成本 |
|---|---|---|
| BrainState 新增字段 | import + 字段存在性探针 | 秒级 |
| `after_analyze`/`after_plan` 路由逻辑 | 纯函数单测（construct state → 断言路由分支） | 秒级 |
| clarify 上限/skip/auto_accept 旁路 | 单测（喂 4 条问题断言截断 3；auto_accept 断言 skip） | 秒级 |
| **graph 重新编译 + interrupt/resume 全链路** | 一次真实 E2E（create_task→clarify interrupt→resume→plan→elaborate），**等到 terminal 态再判成功** | 重，仅此一项 |
| push_planning_feedback | tracing 关闭下 no-op 探针 + tracing 开启下 mock client 断言 key | 秒级 |

> 唯一的重 E2E 留给"interrupt/resume 全链路"——因为它改了 graph 编译和中断语义（harness-semantics 级），值得真跑并等 terminal 态。其余全部轻量。

---

## 7. 待评审的开放问题（实现前需你拍板）

1. **clarify 默认开还是默认关？** 我倾向"默认开 + 阈值 0.5 保守"，但 auto_accept 永不触发。若你希望更克制，可默认关（`clarify_threshold=1.0` 等效关闭）。
2. **渐进明细对 `medium` 要不要也启用？** 我倾向只对 complex/ultra（零回归优先）。
3. **骨架层要不要独立 confirm？** ultra 任务在骨架层加一个轻确认能更早拦错，但多一次交互。可作为 v2。
