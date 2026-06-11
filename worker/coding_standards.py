"""编码规范注入（L2 规范执行金字塔的一层）。

设计理念（CTO 决策，见会话）——规范执行金字塔：
  L0 自动格式化(确定性,免费)      → format_gate.py
  L1 静态闸门 lint(确定性)        → l1_pipeline._lint_files
  L2 分级硬规则注入(本模块)        → 按模型能力裁剪【量】，非内容
  L3 项目规范检索(按需注入)        → knowledge norms layer
  L4 大模型交付复审(语义兜底)      → brain/integration_review

本模块只管 L2：把【机器难以强制、但短小关键】的规范注入 Worker prompt。
绝不把"行长/import 顺序/未用变量"这类塞进来——那是 L0/L1 的确定性职责。

分级原则：不是给大小模型不同【内容】，而是给不同的【量】+ 兜底强度：
  - 小模型(trivial/medium)：极短核心铁律(≤8 条)，重度依赖 L0/L1 兜底
  - 大模型(complex)：核心铁律 + 语言细则，可承载更丰富上下文
"""

from __future__ import annotations

from swarm.types import SubTaskDifficulty

# ── 跨语言核心铁律（所有模型、所有语言都注入；必须极短）──
# 只放"机器查不出 + 后果严重"的语义性非协商项。
_CORE_RULES = [
    "禁止硬编码密钥/密码/token——用环境变量或配置（违反会被安全扫描阻断交付）",
    "改动严格限定在 Scope 内，不顺手改无关文件",
    "保持与周边代码一致的风格和命名，不引入新的风格流派",
    "错误要显式处理或上抛，禁止裸 except/空 catch 吞异常",
    "不删除你不理解的代码或测试",
]

# ── 语言细则（仅大模型/复杂任务注入；小模型靠 L0/L1 工具兜底）──
_LANG_RULES: dict[str, list[str]] = {
    "python": [
        "类型注解齐全（公共函数签名必须有），通过 ruff + mypy",
        "禁止可变默认参数（def f(x=[])）",
        "I/O 与纯逻辑分离，便于测试",
    ],
    "node": [
        "TypeScript 优先，避免 any；通过 eslint + tsc --noEmit",
        "异步用 async/await，不混用 .then 回调地狱",
        "禁止 == 隐式比较，用 ===",
    ],
    "go": [
        "错误必须检查（if err != nil），不丢弃返回的 error",
        "通过 go vet；导出符号写 doc comment",
        "用 context 传递取消/超时，不开无主 goroutine",
    ],
    "rust": [
        "避免 unwrap()/expect() 于可恢复错误，用 ? + Result",
        "通过 cargo clippy -D warnings；不写无理由的 unsafe",
        "所有权清晰，避免不必要的 clone",
    ],
    "java": [
        "资源用 try-with-resources，不裸 close",
        "避免裸类型，用泛型；通过 checkstyle",
        "不吞异常，至少记录日志或上抛",
    ],
}


def _tier_for_difficulty(difficulty) -> str:
    """子任务难度 → 模型能力档位。

    与现有路由对齐：trivial/medium 走较小模型(small)，complex 走大模型(large)。
    小模型给极简规范避免被长指令淹没；大模型给完整规范。
    """
    val = difficulty.value if hasattr(difficulty, "value") else str(difficulty)
    if val == SubTaskDifficulty.COMPLEX.value:
        return "large"
    return "small"


def build_coding_standards_section(subtask, *, language: str = "") -> str:
    """生成注入 Worker prompt 的编码规范段（L2）。

    Args:
        subtask: 子任务（用 difficulty 决定档位，harness.language 决定语言细则）
        language: 显式语言覆盖；留空则取 subtask.harness.language

    Returns:
        Markdown 规范段；小模型只含核心铁律，大模型附加语言细则。
    """
    lang = (language or getattr(getattr(subtask, "harness", None), "language", "") or "").lower()
    tier = _tier_for_difficulty(getattr(subtask, "difficulty", None))

    lines = ["## 📐 编码规范（非协商项）", ""]
    for i, rule in enumerate(_CORE_RULES, 1):
        lines.append(f"{i}. {rule}")

    # 大模型 + 已知语言：附加语言细则
    if tier == "large" and lang in _LANG_RULES:
        lines.append("")
        lines.append(f"### {lang} 细则")
        for rule in _LANG_RULES[lang]:
            lines.append(f"- {rule}")

    # 统一兜底说明：格式/lint 由工具强制，模型无需纠结
    lines.append("")
    if tier == "small":
        lines.append(
            "> 格式化与 lint 由系统自动处理（你无需手动对齐空格/import 顺序），"
            "专注实现正确性与上面的铁律即可。"
        )
    else:
        lines.append(
            "> 格式化(L0)与 lint(L1)由系统确定性强制，风格细节交给工具；"
            "你专注架构清晰、正确性与安全。"
        )
    return "\n".join(lines)
