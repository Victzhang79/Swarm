"""Swarm 核心类型定义 — 全局共享的数据模型"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, TypedDict

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)


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
    PARTIAL = "PARTIAL"                 # 部分交付：部分子任务放弃，已完成的真实落盘（诚实未完成，非 DONE）
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
def _path_scope_match(fp: str, w: str) -> bool:
    """路径感知的 scope 匹配（S2 修复：弃用裸 endswith 双向匹配）。

    旧实现 `fp.endswith(w) or w.endswith(fp)` 有两个漏洞：
      - 越权：scope 'a.py' 放行 'evil/a.py'、'xa.py'；
      - 空串恒真：scope '' 时 ''.endswith() 恒 True，等于全开。
    新规则按【路径段】对齐（与 worker/l1_pipeline.py:_scope_match 同源）：
      1. 规范化(去 ./、统一 /、去首尾 /)；空串直接拒绝；
      2. 完全相等 → 匹配；
      3. w 作为 fp 的祖先目录段(fp 在 w/ 下) → 匹配；
      4. w 是多段路径且作为 fp 的完整尾部段(容忍仓库根前缀) → 匹配；
         单段 basename 不做尾匹配，避免放行任意目录下同名文件。
    """
    def _norm(p: str) -> str:
        p = (p or "").strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p.strip("/")

    f, ww = _norm(fp), _norm(w)
    if not f or not ww:
        return False
    if f == ww:
        return True
    if f.startswith(ww + "/"):
        return True
    if "/" in ww and f.endswith("/" + ww):
        return True
    return False


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
    allow_any: bool = Field(
        default=False,
        description="放行任意路径读写（greenfield/从零创建 或 scope 无法预判时）",
    )

    def is_writable(self, path: str) -> bool:
        if self.allow_any:
            return True
        targets = self.writable + self.create_files + self.delete_files
        return any(_path_scope_match(path, p) for p in targets)

    def is_readable(self, path: str) -> bool:
        if self.allow_any:
            return True
        return self.is_writable(path) or any(
            _path_scope_match(path, p) for p in self.readable
        )

    def is_create(self, path: str) -> bool:
        return any(_path_scope_match(path, p) for p in self.create_files)

    def is_delete(self, path: str) -> bool:
        return any(_path_scope_match(path, p) for p in self.delete_files)

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


class TaskIntent(str, Enum):
    """任务意图分类 — 驱动差异化编排/harness/验收。

    不同意图的工作流根本不同：
    - CREATE 从零写新代码(greenfield)，验收=能构建+测试通过
    - MODIFY 在现有代码上改(默认)，验收=改动正确+不回归
    - DEBUG 排错，工作流=复现失败→定位→修复→回归验证
    - AUDIT 安全审计，不产 diff 而产结构化报告(SAST+依赖+密钥)
    - REFACTOR 重构，验收=行为不变(测试全过)+结构改善
    """
    CREATE = "create"
    MODIFY = "modify"      # 默认，向后兼容
    DEBUG = "debug"
    AUDIT = "audit"
    REFACTOR = "refactor"


class TaskHarness(BaseModel):
    """子任务验证 harness — Brain 编排时精心编写，告诉 Worker【如何验证产出合格】。

    解决核心问题：原来 Worker 只被告知"运行 run_compile/run_tests"，但没有项目
    特定的构建/测试命令，且命令白名单固定(Maven 导向)，导致 Worker 在 Python
    游戏等项目里跑不了验证命令(日志实证"由于命令白名单限制")，只能口头自报通过。

    harness 由 Brain 根据任务+项目语言生成，Worker 据此执行确定性验证，L1 闸门
    也据此跑真实命令而非信 LLM 自报。
    """
    language: str = Field(default="", description="主语言: python/node/java/go/rust 等")
    setup_commands: list[str] = Field(default_factory=list, description="依赖安装/准备命令(如 pip install -r)")
    build_command: str = Field(default="", description="编译/构建命令(解释型语言可为语法检查)")
    test_command: str = Field(default="", description="测试命令(如 python -m pytest -q)")
    lint_command: str = Field(
        default="",
        description="静态检查命令(如 ruff check / go vet / cargo clippy)；L1 静态闸门用",
    )
    typecheck_command: str = Field(
        default="",
        description="类型检查命令(如 mypy / tsc --noEmit)；默认仅警告不阻断",
    )
    sast_command: str = Field(
        default="",
        description="安全静态扫描命令(如 bandit / gosec / semgrep)；AUDIT 意图用",
    )
    failing_test_command: str = Field(
        default="",
        description="DEBUG 意图：复现 bug 的失败用例命令(修复前应失败、修复后应通过)",
    )
    verify_commands: list[str] = Field(
        default_factory=list,
        description="额外验收命令(如 python -c 'import m; assert m.f()' 烟雾测试)",
    )
    extra_whitelist: list[str] = Field(
        default_factory=list,
        description="本任务需放行的命令前缀(并入全局白名单，让上述命令可执行)",
    )
    sandbox_template: str = Field(
        default="",
        description="可选：指定 CubeSandbox 模板ID(预建语言镜像)；留空用默认镜像+setup_commands 运行时装工具链",
    )

    def all_commands(self) -> list[str]:
        cmds = list(self.setup_commands)
        if self.build_command:
            cmds.append(self.build_command)
        if self.test_command:
            cmds.append(self.test_command)
        cmds.extend(self.verify_commands)
        return [c for c in cmds if c]


_SUBTASK_KEY_ALIASES = {
    # LLM 旧键 → 现字段。N-03：模型偶吐 acceptance（字段名是 acceptance_criteria），
    # 默认 extra=ignore 会静默丢弃致验收恒空。把重映射收敛进模型本身（单一事实源），
    # 替代散落在 brain/nodes 的手工补丁。
    "acceptance": "acceptance_criteria",
    "deps": "depends_on",
    "dependencies": "depends_on",
}


class SubTask(BaseModel):
    """一个可独立执行的子任务"""

    @model_validator(mode="before")
    @classmethod
    def _remap_and_warn_extra(cls, data: Any) -> Any:
        """P2：消除 extra=ignore 的"静默丢键"。

        ① 把已知旧键别名重映射到现字段（不丢数据）；
        ② 对仍无法识别的多余键打 warning（可见而非静默吞），便于发现 schema 漂移。
        仅处理 dict 输入（pydantic 也会传模型实例等，非 dict 原样放行）。
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for old, new in _SUBTASK_KEY_ALIASES.items():
            if old in data and new not in data:
                data[new] = data.pop(old)
            elif old in data:
                data.pop(old, None)  # 新键已在，丢弃同义旧键避免冲突
        known = set(cls.model_fields.keys())
        unknown = [k for k in data if k not in known]
        if unknown:
            logger.warning("[SubTask] 忽略未知键(可能 schema 漂移/LLM 变体): %s", unknown)
        return data

    id: str
    description: str
    intent: TaskIntent = Field(
        default=TaskIntent.MODIFY,
        description="任务意图(create/modify/debug/audit/refactor)，驱动差异化编排与验收",
    )
    difficulty: SubTaskDifficulty = SubTaskDifficulty.MEDIUM
    modality: SubTaskModality = SubTaskModality.TEXT
    scope: FileScope
    contract: dict[str, Any] = Field(default_factory=dict, description="共享接口契约")
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list, description="依赖的子任务 ID")
    model_preference: str | None = None
    est_context_tokens: int = Field(
        default=0,
        description="Brain 预估本子任务执行时的输入上下文规模(tokens)；0=未估。"
        "超过预算(SWARM_SUBTASK_CONTEXT_BUDGET，默认150k<本地小模型196k)的会被 elaborate 二次拆分。",
    )
    harness: TaskHarness = Field(
        default_factory=TaskHarness,
        description="验证 harness：如何构建/测试/验收本子任务(Brain 编排时编写)",
    )
    context_snippets: str = Field(
        default="",
        description=(
            "方案A(task 34fab09e)：ELABORATE 预抽取的 scope 文件关键代码片段"
            "（writable 文件的类/方法签名骨架 + readable 参照文件的相关实现），"
            "随 worker prompt 下发，省掉 worker 在沙箱里 cat 探索耗尽迭代步数。"
        ),
    )


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
        """选取下一批可并行派发的子任务。

        【依赖驱动】真正的并行约束是 depends_on DAG，而非 LLM 给的 parallel_groups。
        实践中 LLM 常把本可并行的独立子任务拆进各自的 group（如 [["st-1"],["st-2"]]），
        导致无谓串行。这里改为：派发【所有依赖已满足】的待执行子任务（受 max_concurrent
        截断），不受 LLM 过度保守分组的限制——只要 depends_on 满足就能并行。

        parallel_groups 仅作为「软提示」保留（向后兼容/可视化），不再用于阻断并行。
        """
        remaining = set(dispatch_remaining)
        if not remaining:
            return []

        def _is_ready(task: SubTask) -> bool:
            return task.id not in completed_ids and all(
                d in completed_ids for d in task.depends_on
            )

        # 所有 remaining 中依赖已满足的子任务都可并行派发
        ready = [
            t for t in self.subtasks
            if t.id in remaining and _is_ready(t)
        ]
        return ready[:max_concurrent]

    def all_completed(self, completed_ids: set[str]) -> bool:
        return all(t.id in completed_ids for t in self.subtasks)

    def topological_order(self) -> list[str]:
        """返回子任务 ID 的拓扑序（被依赖者在前，依赖者在后）。

        用于 MERGE 选 rebase base（A-P1-26c）：3-way 失败的重叠冲突应以【依赖上游】
        (被依赖者)为 base 先保留其 diff、把【依赖下游】标记 rebase 重生成——而非按 hunk
        在文件中的出现序任选 base（出现序与依赖无关，可能让上游反被 rebase，破坏地基）。

        Kahn 算法按原始 subtasks 顺序稳定出队；悬空依赖(指向计划外 ID)忽略；存在环时
        把剩余未排序的子任务按原序补在末尾（稳定兜底，绝不丢子任务）。
        """
        ids = [t.id for t in self.subtasks]
        id_set = set(ids)
        # 仅保留指向计划内子任务的依赖边（忽略悬空依赖，避免永远无法出队）
        deps = {t.id: [d for d in t.depends_on if d in id_set and d != t.id] for t in self.subtasks}
        indeg = {i: len(deps[i]) for i in ids}
        ready = [i for i in ids if indeg[i] == 0]
        order: list[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for i in ids:  # 子任务量小，O(n^2) 可接受
                if n in deps[i]:
                    indeg[i] -= 1
                    if indeg[i] == 0:
                        ready.append(i)
        if len(order) < len(ids):  # 环：剩余按原序补全
            seen = set(order)
            order.extend(i for i in ids if i not in seen)
        return order


# ──────────────────────────────────────────────
# Worker 产出
# ──────────────────────────────────────────────
class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class NotRunKind(str, Enum):
    """L1 确定性验证「没跑出结论」时的原因分类（fail-closed 的类型边界）。

    背景：L1 裁决器历史上把「验证没跑」（det_ok is None / 异常跳过 / 工具或清单缺失 /
    infra 串匹配）一律退化为「信模型自报」，这是静默成功的总根。现在「没跑」必须带上
    原因，裁决器据此 fail-closed：

    - BENIGN：真的没东西可验证（空 diff + 无 harness + scope 不期望改动 = 合法 no-op）。
      可保留 LLM 弱信号。
    - BLOCKED：本应验证却跑不起来（pipeline 异常 / 构建工具或工程清单缺失 / 构建命中
      infra 瞬时故障 / diff 抽取失败 / 非空 diff 却解析到 0 文件）。绝不当 PASS——映射为
      transient 失败，走退避重试，耗尽才硬 FAIL。

    缺失/未知一律按 BLOCKED 处理（fail-closed 默认）。
    """
    BENIGN = "benign"
    BLOCKED = "blocked"


class Severity(str, Enum):
    """安全发现严重度（与 CVSS 分级对齐）"""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class SecurityFinding(BaseModel):
    """单条安全审计发现（SAST / 依赖漏洞 / 密钥泄露）"""
    severity: Severity = Severity.MEDIUM
    category: str = Field(default="", description="类别: sast / dependency / secret")
    rule_id: str = Field(default="", description="规则/CWE/CVE 标识(如 CWE-89, CVE-2024-xxx)")
    title: str = Field(default="", description="问题摘要")
    file: str = Field(default="", description="文件路径")
    line: int = Field(default=0, description="行号(0=不适用,如依赖漏洞)")
    tool: str = Field(default="", description="检出工具: bandit/gosec/semgrep/gitleaks/...")
    recommendation: str = Field(default="", description="修复建议")


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
    audit_findings: list[SecurityFinding] = Field(
        default_factory=list,
        description="AUDIT 意图产出：安全审计发现列表(此类任务通常不产 diff)",
    )


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
