# B1 设计文档：brain/nodes.py 单体拆分

状态：**待拍板** · 版本 v0.1 · 2026-06-14
关联：ROADMAP P1 可维护性 · skill `python-module-splitting`

---

## 1. 问题与现状（实测）

`brain/nodes.py` = **2361 行 / 96KB**，单文件聚合：

| 类别 | 数量 | 明细 |
|------|------|------|
| 节点函数（async，graph 装配用） | **14** | analyze / plan / validate_plan / confirm_plan / dispatch / monitor / handle_failure / merge / verify_l2 / verify_l3 / deliver / revision / learn_success / learn_failure |
| 私有 helper | **29** | 文件分类、harness 推断、L1/L2/L3 校验、worker 派发、安全审计等 |
| 顶层常量 | **7** | `_TRIVIAL_HINTS` / `_FILE_PAT` / `_L2_CMD_RE` 等 |

**外部依赖（拆分必须保持不破）**：
- `brain/graph.py` ← `from swarm.brain.nodes import (analyze, ... 14 个节点)`
- `brain/planning_nodes.py` ← `from swarm.brain.nodes import (_brain_profile_prompt, _get_brain_llm, _parse_json_from_llm)`
- **测试**：~15 个测试文件大量 `from swarm.brain.nodes import X` + **`patch("swarm.brain.nodes._get_brain_llm" / "._dispatch_to_worker" / "._get_project_path" / "._try_l2_sandbox_verify" / "._verify_l2_via_llm" ...)`**

> **价值类型**：纯可维护性，**零功能收益**。risk > 短期 ROI。做它的理由是长期健康（2361 行单文件难以并行开发/定位/审查）。

---

## 2. 头号风险（skill 的头号杀手，已实测确认）

测试用**绝对路径 mock 锚点** `patch("swarm.brain.nodes._X")`。若把 `analyze` 拆到 `brain/nodes/analyze.py`、它内部调 `_get_brain_llm`，而测试 patch 的是 `swarm.brain.nodes._get_brain_llm` —— **patch 不到 analyze.py 模块里的名字绑定**，函数走真实 LLM → 测试崩。

这是"移动符号静默破坏 mock"，是本类任务**最致命**的坑。

---

## 3. 方案：拆包 + `__init__.py` re-export 保 import/patch 路径 100% 不变

把 `brain/nodes.py` → `brain/nodes/` 包：

```
brain/nodes/
├── __init__.py      # re-export 全部公共节点 + 被测试 patch 的私有符号 → swarm.brain.nodes.X 路径不变
├── shared.py        # 无状态纯 helper + 常量（_FILE_PAT/_parse_json_from_llm/_infer_harness 等）
├── analyze.py       # analyze 节点 + 其专属 helper
├── plan.py          # plan/validate_plan/confirm_plan + _build_simple_plan 等
├── dispatch.py      # dispatch/monitor + _dispatch_to_worker/_run_security_audit
├── verify.py        # verify_l2/verify_l3 + L2/L3 校验 helper
├── recover.py       # handle_failure（202 行，失败恢复）
├── merge.py         # merge + _make_base_reader
├── deliver.py       # deliver/revision
└── learn.py         # learn_success/learn_failure
```

### 关键不变量（零破坏的核心）
1. **`brain/nodes/__init__.py` 显式 re-export 所有符号**：
   ```python
   from swarm.brain.nodes.analyze import analyze
   from swarm.brain.nodes.shared import _get_brain_llm, _parse_json_from_llm, _brain_profile_prompt, ...
   from swarm.brain.nodes.dispatch import _dispatch_to_worker, _run_security_audit
   # ... 所有被 import 或被 patch 的符号
   ```
   → `from swarm.brain.nodes import analyze` 不变；graph.py / planning_nodes.py 一行不改。

2. **🔑 mock 锚点的真正修复（不是 re-export 能解决的）**：
   测试 `patch("swarm.brain.nodes._get_brain_llm")` patch 的是 `__init__` 命名空间的名字。但 `analyze.py` 里 `from .shared import _get_brain_llm` 后，`analyze` 调的是 `analyze.py` 模块的绑定，**patch __init__ 不影响它**。
   **解法**：节点模块内部对"被测试 patch 的 helper"采用**模块限定调用**而非直接名字 —— 即 `analyze.py` 里写 `from swarm.brain import nodes` 然后 `nodes._get_brain_llm(...)`，或保留 helper 在被 patch 的同一命名空间。
   **更稳妥**：先逐个测试核对其 patch 目标，对每个被 patch 的符号，确保"调用点"和"patch 点"在同一模块。具体做法分批验证（见 §4）。

3. **被 patch 的有状态 helper（`_dispatch_to_worker` / `_get_project_path` / `_get_brain_llm`）** 与其调用方放同一子模块，避免跨模块 patch 失效。

### 迁移策略（skill 的 AST 脚本化）
- git 安全网（当前 718 绿 = 基线，已提交）
- AST 脚本按行号区间切块搬移（不逐行 LLM 重写）；删原定义从后往前删
- **每抽一个子模块**：清 `__pycache__` → 跑该域测试 → 跑全量 → 确认计数 ≥ 718
- 增量逐域，任何中途叫停代码库都一致

---

## 4. 分批计划（每批独立可回滚）

| 批 | 内容 | 验证 |
|----|------|------|
| **批1** | 建 `brain/nodes/` 包，`nodes.py`→`nodes/__init__.py`（先整体搬，re-export 自己），跑全量确认 718 不变 | 路径不变、计数不变 |
| **批2** | 抽 `shared.py`（无状态纯 helper + 常量），其余留 __init__ | shared 域测试 + 全量 |
| **批3** | 抽 analyze/plan（含 mock 锚点核对：_get_brain_llm patch 仍生效） | test_knowledge_brain / test_*planning* |
| **批4** | 抽 dispatch/verify（含 _dispatch_to_worker / _try_l2_* patch 核对） | test_brain_phase3 / test_audit_orchestration |
| **批5** | 抽 recover/merge/deliver/learn | test_p0_path / test_learn_chain / test_simple_retry_cap |

每批一个 commit；全批齐 bump minor（0.7.x → 视情况）。

---

## 5. 待确认疑问（请拍板）

| # | 疑问 | 选项 / 我的建议 |
|---|------|----------------|
| **Q1** | **现在做 vs 推迟**？skill 明确建议"有一次反正要大改 brain 的功能迭代时顺带拆，不单独为拆而拆"。当前无 brain 功能迭代在途。 | A. 现在就拆（纯投资）；B. 推迟到下次动 brain 时顺带（skill 建议）。**我倾向先把 dispatch/verify 这种最大、最常改的域拆出来（部分拆分），shared 抽出，其余暂留** —— 取 80% 收益、20% 风险 |
| **Q2** | **拆分粒度**：按上面 9 个子模块全拆，还是先粗拆 3-4 个大域（analyze+plan / dispatch+verify+recover / merge+deliver+learn / shared）？ | 我倾向**粗拆**（域少、import 关系简单、回归面小），后续需要再细分 |
| **Q3** | **mock 锚点处理方式**：(a) 改测试的 patch 路径指向新模块；(b) 节点内部用 `nodes.helper()` 模块限定调用让旧 patch 仍生效（测试零改）。 | 我强烈倾向 **(b) 测试零改** —— 测试是回归安全网，动它本身有风险；让生产码适配测试合约 |
