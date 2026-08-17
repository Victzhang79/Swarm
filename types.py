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
    POOLED = "POOLED"                  # 需求池：仅登记不执行，等手动触发（D59 补全）
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    CLARIFYING = "CLARIFYING"          # 澄清问答人工闸（D59 补全，SSOT=task_states.py）
    DESIGN_REVIEW = "DESIGN_REVIEW"    # 技术方案评审人工闸（D59 补全，SSOT=task_states.py）
    VALIDATING_PLAN = "VALIDATING_PLAN"
    CONFIRMING = "CONFIRMING"          # 等人工确认
    DISPATCHING = "DISPATCHING"
    MONITORING = "MONITORING"
    HANDLING_FAILURE = "HANDLING_FAILURE"
    MERGING = "MERGING"
    VERIFYING_L2 = "VERIFYING_L2"
    VERIFYING_RUNTIME = "VERIFYING_RUNTIME"  # S1-4 运行时冒烟闸门（D59 不变量：TaskStatus 须覆盖 SSOT=task_states.py）
    VERIFYING_L3 = "VERIFYING_L3"      # L3 GitLab CI 验证（D59 补全，SSOT=task_states.py）
    DELIVERING = "DELIVERING"
    IN_REVISION = "IN_REVISION"
    LEARNING_SUCCESS = "LEARNING_SUCCESS"
    LEARNING_FAILURE = "LEARNING_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PARTIAL = "PARTIAL"                 # 部分交付：部分子任务放弃，已完成的真实落盘（诚实未完成，非 DONE）
    DONE = "DONE"

    # 终态分类单一事实源：PARTIAL 是终态（任务已收敛、不再推进）但【非成功】（诚实未完成）。
    # 各处硬编码 ("DONE","FAILED","CANCELLED") 元组历史漏 PARTIAL → 统计洗白/SSE 悬挂/去重误判。
    @classmethod
    def is_terminal_status(cls, status: "str | TaskStatus") -> bool:
        s = status.value if isinstance(status, cls) else str(status)
        return s in (cls.DONE.value, cls.FAILED.value, cls.CANCELLED.value, cls.PARTIAL.value)

    @classmethod
    def is_successful_status(cls, status: "str | TaskStatus") -> bool:
        """仅 DONE 算成功。PARTIAL/FAILED/CANCELLED 皆非成功。"""
        s = status.value if isinstance(status, cls) else str(status)
        return s == cls.DONE.value


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
    upstream_artifacts: list[str] = Field(
        default_factory=list,
        description=(
            "本子任务 readable 里【由上游/兄弟子任务 create_files 传播进来的产物】子集。"
            "是 readable 的确定性 provenance 标记（区别于基线只读上下文），供 worker "
            "bootstrap 做 fail-closed：这些产物缺失于本地=上游未就绪/被放弃 revert，"
            "不把破工作区交 worker 空烧，先判 BLOCKED 等生产者。"
        ),
    )
    upstream_products: list[str] = Field(
        default_factory=list,
        description=(
            "C6/H-1（批次6）：【完成态上游产物 ∩ 本任务 writable】账——dispatch 每次派发"
            "按当前 L1 通过子任务 diff 全集【重算替换】（非累加，防粘滞 provenance 把"
            "非上游文件永久赦免出防脏 reset）。消费端=worker reset 跳过 + clean_upload "
            "传本地已合并版（executor_sync）。与 upstream_artifacts 分工：后者是"
            "plan 布线+完成态的累加账（seed 闸/readable 补传消费），本字段是"
            "完成态即时账（防脏护栏豁免消费）。"
        ),
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


# ★30 号文批10（C-4/C-5）：needs_review reason 单一事实源★
# worker 写入侧（worker/l1_pipeline.py L1 闸门收尾判据）与 brain 终态未核验账消费侧
# （brain/runner.py _failed_machine_account）必须共用同一份枚举——两处各写字面量，
# 新 reason 加了没人读＝空账（硬检查第四条：新账必须有人消费）。
# 另一个消费面 brain/nodes/__init__.py:_collect_needs_review 是 reason-agnostic 聚合
# （任何 truthy 值都收），不在此枚举约束内。
NEEDS_REVIEW_REASONS: tuple[str, ...] = (
    "no_test_or_verify_commands",      # 非空 diff 但既无 test 命令也无 verify 断言（零语义覆盖）
    "verify_all_skipped_h1",           # verify 清单非空但全部被 H1 模板覆写跳过（零命令真跑）
    "test_skipped_manifest_missing",   # 给了 test 命令但工程清单缺失被跳过（如 npm test 无 package.json）
    "test_skipped_no_npm_script",      # npm test 但 package.json 无 scripts.test（W-7 出口）
    "test_skipped_no_tests_collected", # pytest rc=5：命令适用但该目录零用例（猜错的命令≠代码坏了）
    "coverage_capped",                 # 文件数超 SWARM_WORKER_L1_MAX_FILES，编译/lint 只覆盖前 N 个
    # ★31 号文 A3-M1★ 改动含只能靠 tsc/vue-tsc 的文件（.ts/.tsx/.mts/.cts/.vue/.jsx）而
    # tsc 未给出通过裁决（未装 typescript / 无 package.json / infra 跳过）⇒ 那些文件本轮
    # **零语法与类型覆盖**。node --check 解析不了 TS 语法故无法兜底；补不了闸但绝不静默。
    "ts_gate_unavailable",
    # ★31 号文 A3-M3★ 清单探针（沙箱 find）失败 ⇒ `_manifest_present` 保守返 False，而
    # compile/lint 消费侧把 False 当"该闸不适用" ⇒ 闸可能整段跳过。探针失败与"清单真不在"
    # 治前完全同形；现成账并接本通道，让"闸没跑"在终态可机读。
    "manifest_probe_failed",
)


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
    covers: list[str] = Field(
        default_factory=list,
        description="S2-2：本子任务覆盖的需求条目 ID（req-<sha1[:8]>，见 state.requirement_items）。"
        "PLAN LLM 声明、plan validator 覆盖矩阵校验消费（task#24）。加法兼容字段："
        "旧 checkpoint/旧 LLM 输出无此键=默认空列表，绝不影响既有链路。",
    )
    depends_on: list[str] = Field(default_factory=list, description="依赖的子任务 ID")
    model_preference: str | None = None
    retry_guidance: str = Field(
        default="",
        description="A4(round11)：重试时由 HANDLE_FAILURE 注入的 brain 失败诊断/硬约束，"
        "worker prompt 渲染为'上次失败的诊断与约束'块，防换模型重试仍重蹈同类错误。每次重试覆写。",
    )
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
def _edge_norm_path(f) -> str:
    """路径归一（与 contract_utils._norm_scope_path 同口径；types 为底层不可反向 import，
    此处内联同一规则——两处必须保持一致，改动须同步。批次2 闸门 reviewer R2 LOW-1：
    尾斜杠剥离随 SSOT 同步）。"""
    p = str(f).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/").rstrip("/")


def edge_is_soft(consumer: "SubTask", producer: "SubTask | None") -> bool:
    """R65REPLAY-T1（round65d 回放 C 路反事实实锤：消费边把连坐闭包 15→72）：
    依赖边 consumer→producer 的【软序/硬依赖】结构性判定（零簿记——存储标记方案在
    重拆 remap/plan rebuild/注入重推导处必漏，st-2→st-11-1 软边被 remap 成→分片即实锤）。

    软序边 ⇔ producer 产出(create∪writable) ∩ consumer.upstream_artifacts = ∅
             且 producer.create_files ∩ consumer.readable ≠ ∅
             且 producer.writable    ∩ consumer.readable = ∅
    语义：readable 驱动的"我想读你将【新建】的文件"只值一个调度顺序——生产者死了
    文件不存在，R49-2 幻影过滤运行期剔除、消费者不会读到垃圾，可越过尝试（L1 裁决）。
    ★复核 F1（hunter HIGH）：consumer.readable 与 producer.writable 有交=消费者要读
    的是【存量文件的改造后版本】——生产者死了文件仍在盘上（改造前旧版），R49-2 只查
    存在性兜不住，越过=静默拿旧接口写代码 → 判硬★。ua 有交=seed 闸构建输入=硬；
    R65TR-T3：生产者产出∩消费者写集有交（无任何生产关系）=【共写序边】（pom 写权
    授予串边/规则1.5 共享写者串行链）=软——只序不连坐，并发写由写集锁+MERGE 收口；
    其余零交集=LLM/脚手架结构边（理由未知）=保守硬；producer 悬空（不在 plan，如重拆
    旧 id）无法判定=保守硬。栈中立（纯路径/集合逻辑）。
    """
    if producer is None:
        return False
    psc = getattr(producer, "scope", None)
    if psc is None:
        return False
    created = {_edge_norm_path(f) for f in (getattr(psc, "create_files", None) or [])}
    modified = {_edge_norm_path(f) for f in (getattr(psc, "writable", None) or [])}
    if not (created or modified):
        return False
    csc = getattr(consumer, "scope", None)
    if csc is None:
        return False
    ua = {_edge_norm_path(f) for f in (getattr(csc, "upstream_artifacts", None) or [])}
    if (created | modified) & ua:
        return False
    rd = {_edge_norm_path(f) for f in (getattr(csc, "readable", None) or [])}
    if modified & rd:
        return False   # 读存量文件的改造后版本：死产者留下旧版在盘上，越过=静默陈旧
    if created & rd:
        return True
    # R65TR-T3（对抗双复核 2×HIGH 实弹）：共写序边=软。生产者产出与【消费者写集】
    # 有交、且上方两臂已排除一切生产关系（ua 构建输入/readable 消费）——两任务只是
    # 共写同一文件的写序列化边（pom 写权授予串边/规则1.5 共享写者串行链均此形）。
    # 只序不连坐：死者未产出消费者要消费的任何东西；判硬实测会让 grantee 之死把
    # 健康共写者拖进 revert 级联（阶梯三无 completed_ids 保护路径=已完成产物被
    # git 还原的数据损失面）。并发写由 E3 写集锁+MERGE 收口。其余零交叠（理由
    # 未知的 LLM/结构边）维持保守硬不变。
    cw = {_edge_norm_path(f) for f in (
        list(getattr(csc, "create_files", None) or [])
        + list(getattr(csc, "writable", None) or []))}
    return bool((created | modified) & cw)


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
    # R41 复核 F1：确定性收尾器/缺件外科挂靠的孤儿文件记录 {subtask_id: [paths]}。
    # #6 覆盖单调化按 scope 文件身份跨轮配对，挂靠会让 scope 键漂移（挂靠轮 vs 全量
    # 重拆轮键不等 → covers 静默丢失不收敛）——配对两侧对称减去本记录即可还原 LLM
    # 原始 scope 身份。随 plan 持久化（老 checkpoint 缺字段=默认空，零迁移）。
    finisher_attached: dict[str, list[str]] = Field(
        default_factory=dict,
        description="收尾器/外科确定性挂靠的孤儿文件（scope 身份配对时剔除）",
    )
    # R67J-H3b：R67-T4b 符号消费补边时因成环放弃的 (消费者, 生产者) 对。DAG 上生产者
    # （传递）依赖消费者 → 边不可加；本账供 get_dispatch_batch 软序兜底（放弃/软边松弛
    # 后二者同批就绪时生产者先行）+ 观测。加法兼容：老 checkpoint 缺字段=默认空。
    symbol_cycle_pairs: list[list[str]] = Field(
        default_factory=list,
        description="成环放弃的符号消费对 [[消费者id, 生产者id], …]（软序账，不改依赖图）",
    )
    # ★31 号文 A1-M2★ B3④ 剔除成环符号正断言的账。治前唯一痕迹是一条 WARNING ⇒ 纪律 #106
    # 明令进度/状态判读绝不解析 swarm.log ⇒ "这个子任务的验收面被确定性拿掉了"在机读面
    # 完全不可见（实测：剔到 0 条时 `wire_symbol_consumption_edges` 返回 {}、plan 无任何属性）。
    # ★为什么不能用 symbol_cycle_pairs 反推★：环对存在 ≠ 有断言被剔（该消费者可能本来就
    # 没正断言），两者不可互相推导——这正是"同一事实的第二个标签"反面：**不同事实**不得
    # 共用一个账。加法兼容：老 checkpoint 缺字段=默认空。
    symbol_exam_dropped: dict[str, list[str]] = Field(
        default_factory=dict,
        description="B3④ 因成环剔除的验收正断言 {子任务id: [被剔断言, …]}（fail-honest 账）",
    )
    symbol_exam_zeroed: list[str] = Field(
        default_factory=list,
        description="B3④ 剔除后验收面【归零】的子任务 id（与'剔了一部分'必须可区分）",
    )

    def get_ready_tasks(
        self, completed_ids: set[str], abandoned: set[str] | None = None
    ) -> list[SubTask]:
        """获取当前可执行的子任务（依赖已全部完成）。

        abandoned=【已永久放弃的子任务集】(阶梯三打桩/revert/连坐)：本身被放弃的、或【硬】
        依赖了放弃项的子任务【永不就绪】，绝不再派发——杜绝"依赖永远不会落地的下游"被反复
        重派。R65REPLAY-T1：软序边（edge_is_soft，readable 驱动消费）的死产者视为已满足
        ——消费者可越过尝试，不陪葬；活产者软硬边照常等待。
        """
        _ab = abandoned or set()
        _by_id = {t.id: t for t in self.subtasks}

        def _dep_ok(t: "SubTask", d: str) -> bool:
            if d in completed_ids:
                return True
            return d in _ab and edge_is_soft(t, _by_id.get(d))

        return [
            t for t in self.subtasks
            if t.id not in completed_ids and t.id not in _ab
            and all(_dep_ok(t, d) for d in t.depends_on)
        ]

    def get_dispatch_batch(
        self,
        completed_ids: set[str],
        dispatch_remaining: list[str],
        max_concurrent: int,
        abandoned: set[str] | None = None,
        deprioritized: set[str] | None = None,
        in_flight: set[str] | None = None,
        dispatch_totals: dict | None = None,
    ) -> list[SubTask]:
        """选取下一批可并行派发的子任务。

        【依赖驱动】真正的并行约束是 depends_on DAG，而非 LLM 给的 parallel_groups。
        实践中 LLM 常把本可并行的独立子任务拆进各自的 group（如 [["st-1"],["st-2"]]），
        导致无谓串行。这里改为：派发【所有依赖已满足】的待执行子任务（受 max_concurrent
        截断），不受 LLM 过度保守分组的限制——只要 depends_on 满足就能并行。

        parallel_groups 仅作为「软提示」保留（向后兼容/可视化），不再用于阻断并行。

        【Fix F·dispatch 前进保证（解 head-of-line 死锁）】`deprioritized` = 已尝试过并
        失败、正在重试中的子任务集（由 dispatch 按 subtask_retry_counts>0 计算）。失败撮常
        撞 900s worker 超时，且早序、恒就绪 → 旧 `ready[:max_concurrent]` 让它们每批霸占
        并发槽，把「从未尝试的就绪生产者（新前沿）」饿死 → 完成数冻结（15 轮无一到 MERGE 的
        真根因）。这里做**纯优先级重排（非丢弃、非跳过）**：fresh（新前沿）优先占槽，retry
        仅填剩余槽位。生产者先跑→合并→失败子任务下轮重试即真恢复（round15 st-19-1 实证）。
        稳定性：两组各自保持 self.subtasks 序 → 确定性；deprioritized 为空则完全等价旧行为。
        不改放弃集/熔断语义：retry 仍在 remaining 且仍可派发，只是不再独占槽。

        【R67L-B4①·retry 组内饥饿者优先/死结降权】（22号文批次4，round67l 骨牌2 实锤）：
        retry 组纯保 plan 原序时，早序死结 id（st-2/8/14 终身派发 4 轮仍失败）恒占组头，
        max_concurrent 截断下 retry 判词兑现者（st-73 等 11 个终身派发仅 1 次）零槽位饿死
        70min，终态只能 dispatched_unaccounted 认账。`dispatch_totals`=A2 终身派发账
        （单调不剪枝，subtask_dispatch_totals）——retry 组按其升序重排：少派=更饿先占槽，
        多派死结沉底填剩余槽。稳定排序保同次数者 plan 原序确定性；fresh 组语义（Fix F
        新前沿优先）不变。死结降权非放弃：totals 大者仍在组尾，不剥夺重试资格；
        dispatch_totals 缺省（None）=完全等价旧行为。
        """
        remaining = set(dispatch_remaining)
        if not remaining:
            return []
        _ab = abandoned or set()
        _dp = deprioritized or set()
        _by_id = {t.id: t for t in self.subtasks}

        def _is_ready(task: SubTask) -> bool:
            # 已放弃的子任务、或依赖了【无产出】放弃项的下游 → 永不就绪（其依赖永远
            # 不会落地），不再被选中派发，斩断 BLOCKED→replan→复活 的无界循环。
            # 4.9 复核 T5（CONFIRMED）：completed 优先于放弃集——阶梯三【打桩路】的
            # 生产者同时在 give_up 与 completed（桩产出 l1_passed=True），设计意图=让
            # 下游对可编译桩照常推进；先查放弃集会把带依赖边的下游永久扣死→被
            # #R13-4 静默划进 PARTIAL。revert 路（无产出）不在 completed，语义不变。
            if task.id in _ab:
                return False
            # R65REPLAY-T1：死产者（放弃且无产出）——硬依赖扣死（旧语义）；软序边
            # （edge_is_soft，readable 驱动消费）视为已满足，消费者可越过尝试。
            for d in task.depends_on:
                if d in _ab and d not in completed_ids \
                        and not edge_is_soft(task, _by_id.get(d)):
                    return False
            return task.id not in completed_ids and all(
                d in completed_ids
                or (d in _ab and edge_is_soft(task, _by_id.get(d)))
                for d in task.depends_on
            )

        # 所有 remaining 中依赖已满足的子任务都可并行派发
        ready = [
            t for t in self.subtasks
            if t.id in remaining and _is_ready(t)
        ]
        fresh = [t for t in ready if t.id not in _dp]
        retry = [t for t in ready if t.id in _dp]
        # B6（round48c 实锤）：收尾器注入的 st-scaffold-*/st-contract-* 在 subtasks
        # 尾部 → 列表序派发让结构上游等了 4.4h，派到时环境已被毒死。fresh 内按
        # 结构优先级稳定重排：①纯构建清单子任务（脚手架——模块存在性是全场地基）
        # ②被依赖的生产者 ③其余。稳定排序保确定性；retry 组语义不变（Fix F）。
        _manifest_names = ("pom.xml", "settings.gradle", "settings.gradle.kts",
                           "build.gradle", "build.gradle.kts", "cargo.toml", "go.work")
        _dep_counts: dict[str, int] = {}
        for t in self.subtasks:
            for d in t.depends_on:
                _dep_counts[d] = _dep_counts.get(d, 0) + 1

        def _prio(t: SubTask) -> tuple[int, int]:
            # R65D-W2②：同层内按下游扇出降序——高位生产者（round65d st-26 扇出 90）
            # 晚跑一轮=全场多等一轮。稳定排序保同扇出者原序确定性。
            _fan = _dep_counts.get(t.id, 0)
            files = [str(f).rsplit("/", 1)[-1].lower()
                     for f in (list(t.scope.create_files) + list(t.scope.writable))]
            if files and all(f in _manifest_names or f.endswith(".sln")
                             for f in files):
                return (0, -_fan)
            return (1, -_fan) if _fan > 0 else (2, 0)

        fresh.sort(key=_prio)  # list.sort 稳定：同级同扇出保持 subtasks 原序
        # R67L-B4①：retry 组内饥饿者优先——按终身派发次数升序（少派=更饿先占槽，死结沉底）。
        # list.sort 稳定：同次数保持 subtasks 原序确定性。dispatch_totals 缺省=旧行为。
        if dispatch_totals:
            retry.sort(key=lambda t: int(dispatch_totals.get(t.id, 0) or 0))
        ready = fresh + retry
        # R67J-H3b：符号成环对软序兜底——(消费者 c, 生产者 p) 因成环放弃补边（p 传递依赖
        # c，DAG 序=c 先行），正常情形二者绝不同批就绪；仅当依赖被放弃/软边旁路松弛后才会
        # 同批可派。此时 defer c 一批让 p 先落产物，省 c 的一次必然 BLOCKED 白跑。
        # 护栏：①只 defer 不丢派（c 仍在 remaining，下批 p 完成/在飞后自然释放）；
        # ②p 不在本轮就绪集也不在飞 → 绝不 defer（p 传递依赖 c 的原生环下 defer=调度自死锁）；
        # ③互对/链式对按 sorted 确定性处理，"p 未被 defer 才 defer c"保证至少一者保留
        # （反证：若全被 defer，最后一次 defer 时其 p 未被 defer 且此后无人再 defer 它）；
        # ④复核 M2 整改：`in_flight`=已派出未完成集（滚动补位传入）——p 在飞时 c 同样要
        # defer，否则补位轮 p 不在 remaining → 不在就绪集 → 绕过软序，c 与执行中的 p
        # 并跑（正是本机制要防的"消费者先于生产者产物"）。p 在飞者绝不参与"互 defer"
        # 判定（它不在本批候选，defer 它无意义也无饿死风险——在飞必然终结）。
        _cyc_pairs = list(getattr(self, "symbol_cycle_pairs", None) or [])
        if _cyc_pairs:
            _ready_ids = {t.id for t in ready}
            _in_flight = in_flight or set()
            _deferred: set[str] = set()
            for _pr in sorted(tuple(p[:2]) for p in _cyc_pairs if len(p) >= 2):
                _c, _p = str(_pr[0]), str(_pr[1])
                if _c in _ready_ids and _c not in _deferred and (
                        (_p in _ready_ids and _p not in _deferred)
                        or _p in _in_flight):
                    _deferred.add(_c)
            if _deferred:
                # 猎手复核整改：defer 决策必须留痕——被 defer 者不进 to_dispatch，
                # dispatch_totals=0 连账龄追踪都不覆盖，无日志=完全黑箱。
                logger.info(
                    "[DISPATCH] R67J-H3b 软序 defer %d 个成环消费者让生产者先行"
                    "（只延批不丢派）: %s", len(_deferred), sorted(_deferred))
                ready = [t for t in ready if t.id not in _deferred]
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
    scope_objection: dict[str, Any] | None = Field(
        default=None,
        description=(
            "B4-2（round38c）：worker 对 scope 的结构化异议 {file, reason, suggested}——"
            "认定 create_files 里某文件名/路径本身错误（撞框架类名/包路径不符）时上抛，"
            "HANDLE_FAILURE 消费（校验后替换 scope 条目），替代 notes 散文无人读的死通道"
        ),
    )
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
    # 批5：hybrid_ranked_files/hybrid_scores 已删（产出方 _apply_hybrid_fusion 无生产读者）


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
