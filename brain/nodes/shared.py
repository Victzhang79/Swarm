"""brain/nodes/shared.py — Brain 节点的无状态纯 helper + 常量（B1 批2 抽出）。

只放无状态、无 mock 依赖的纯函数与常量（skill 原则）。被测试 patch 的有状态符号
（_get_brain_llm / _dispatch_to_worker / _get_project_path）仍留在 __init__.py。
__init__.py 通过 `from .shared import *` 等价 re-export，保 swarm.brain.nodes.X 路径不变。
"""

from __future__ import annotations

import json
import os
import re

from swarm.brain.state import BrainState
from swarm.types import (
    Complexity,
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskHarness,
    TaskIntent,
    TaskPlan,
)


_FILE_EXT = (
    r"py|js|jsx|ts|tsx|java|go|rs|rb|php|c|cc|cpp|h|hpp|cs|kt|swift|scala|"
    r"sh|md|rst|txt|toml|yaml|yml|json|ini|cfg|env|xml|html|css"
)


_FILE_PAT = re.compile(
    rf"(?<![A-Za-z0-9_/.\-])([A-Za-z0-9_][A-Za-z0-9_./\-]*\.(?:{_FILE_EXT}))(?![A-Za-z0-9_./\-])"
)


_CREATE_HINTS = ("新建", "新增", "创建", "添加文件", "生成", "输出", "create", "add file", "new file", "生成一个", "写一个", "写个", "实现一个", "做一个", "开发")


_DELETE_HINTS = ("删除", "移除", "去掉", "删掉", "delete", "remove")


def _guess_target_files(task_description: str) -> list[str]:
    """从需求中抠出 ASCII 文件名（中文不会粘连）。"""
    return list(dict.fromkeys(m.group(1) for m in _FILE_PAT.finditer(task_description)))


def _classify_file_ops(task_description: str) -> dict[str, list[str]]:
    """把需求里点名的文件按操作意图分类: modify / create / delete。

    启发式：在文件名附近（同一子句）出现删除/新建关键词则归类，否则默认 modify。
    用中文/标点/英文动词切分子句，逐句判断该句里的文件归哪类。
    """
    create: list[str] = []
    delete: list[str] = []
    modify: list[str] = []
    # 按常见分隔符切子句，让"删除 a.py，新增 b.py"能分别归类。
    # 注意：不能用 '.' 当分隔符（会切断 readme.md）；中文句号'。'可以。
    clauses = re.split(r"[，,；;。\n、]| and | then |然后|以及|并且|并|再|同时", task_description)
    for clause in clauses:
        files = _guess_target_files(clause)
        if not files:
            continue
        low = clause.lower()
        if any(h in clause or h in low for h in _DELETE_HINTS):
            delete.extend(files)
        elif any(h in clause or h in low for h in _CREATE_HINTS):
            create.extend(files)
        else:
            modify.extend(files)
    # 去重 + 互斥优先级：delete > create > modify（同名只归最强意图）
    delete = list(dict.fromkeys(delete))
    create = list(dict.fromkeys(f for f in create if f not in delete))
    modify = list(dict.fromkeys(f for f in modify if f not in delete and f not in create))
    return {"modify": modify, "create": create, "delete": delete}


def _format_project_structure(knowledge_context: dict | None) -> str:
    """从知识上下文(codegraph struct 层)提炼真实文件/符号清单，供 LLM 拆分参考。

    让大模型基于真实存在的文件分配 scope，而非凭需求文字臆造文件名。
    """
    if not knowledge_context:
        return "（无项目结构索引——可能是新项目或预处理未完成，请根据需求合理新建文件）"
    struct = knowledge_context.get("struct") or []
    if not struct:
        return "（结构索引为空，请根据需求合理命名新建文件）"
    by_file: dict[str, list[str]] = {}
    for item in struct:
        fp = item.get("file_path") or item.get("file") or ""
        name = item.get("symbol_name") or item.get("name") or ""
        if not fp:
            continue
        by_file.setdefault(fp, [])
        if name and len(by_file[fp]) < 8:
            by_file[fp].append(name)
    if not by_file:
        return "（结构索引为空，请根据需求合理命名新建文件）"
    lines = []
    for fp in sorted(by_file)[:25]:  # 限制文件数，避免 prompt 膨胀
        syms = ", ".join(by_file[fp])
        lines.append(f"- {fp}" + (f"  (符号: {syms})" if syms else ""))
    extra = "" if len(by_file) <= 25 else f"\n…… 等共 {len(by_file)} 个相关文件"
    return "\n".join(lines) + extra


def _infer_harness(task_description: str, scope, project_path: str = "") -> "TaskHarness":
    """根据任务描述/scope 文件/项目结构推断一个合理的验证 harness。

    用于 SIMPLE 快速路径，以及 LLM plan 未给出 harness 时的兜底。按语言给出
    构建/测试/验收命令 + 需放行的命令白名单，让 Worker 能真正跑验证而非口头自报。
    """
    # 收集 scope 内文件后缀判断语言。
    # 关键：优先用【可写/新建】文件(子任务实际产出的语言)，readable 仅在前者
    # 无后缀时兜底——混编项目里 readable 常含其他语言的上下文文件，会误判。
    produced: list[str] = []
    for attr in ("writable", "create_files"):
        produced.extend(getattr(scope, attr, []) or [])
    readable = list(getattr(scope, "readable", []) or [])
    primary_files = produced if any("." in f for f in produced) else (produced + readable)

    # 按【主导语言】判定，而非"任一后缀命中即算"——混编 scope 里夹带的少量
    # 其他语言文件(如 87 个 .java 里混 1 个 .js)不应让整体被误判为 node。
    # 统计各语言扩展名计数，取最多的作为主导语言。
    _LANG_EXTS = {
        "python": {"py"},
        "node": {"js", "jsx", "ts", "tsx", "vue", "svelte", "mjs", "cjs"},
        "java": {"java", "kt"},
        "go": {"go"},
        "rust": {"rs"},
    }
    _ext_counts: dict[str, int] = {}
    for f in primary_files:
        if "." not in f:
            continue
        e = f.rsplit(".", 1)[-1].lower()
        for lang, langexts in _LANG_EXTS.items():
            if e in langexts:
                _ext_counts[lang] = _ext_counts.get(lang, 0) + 1
                break
    dominant_lang = max(_ext_counts, key=lambda k: _ext_counts[k]) if _ext_counts else None
    text = (task_description or "").lower()

    def has(*kw: str) -> bool:
        return any(k in text for k in kw)

    def is_lang(lang: str, *kw: str) -> bool:
        """主导语言匹配优先；无主导语言(scope 无代码文件)时回退描述关键词。"""
        if dominant_lang is not None:
            return dominant_lang == lang
        return has(*kw)

    # 语言判定（scope 后缀优先，其次描述关键词）→ 完整工具链矩阵
    # 每语言给出 build/test/lint/typecheck/sast + setup(运行时装工具) + 白名单。
    # 工具缺失时由 L1/审计层优雅降级，这里只声明"理想命令"。
    if is_lang("python", "python", "pytest", "django", "flask", "fastapi", "pygame"):
        return TaskHarness(
            language="python",
            setup_commands=["pip install -q ruff bandit pip-audit 2>/dev/null || true"],
            build_command="python -m compileall -q .",
            test_command="python -m pytest -q",
            lint_command="ruff check .",
            typecheck_command="mypy . --ignore-missing-imports",
            sast_command="bandit -r . -ll -f json",
            extra_whitelist=[
                "python", "python3", "python -m", "python -c",
                "pytest", "ruff", "mypy", "bandit", "pip-audit", "pip install",
                "ls", "cat",
            ],
        )
    if is_lang("node", "node", "npm", "react", "typescript", "vue"):
        return TaskHarness(
            language="node",
            setup_commands=["npm ci 2>/dev/null || npm install 2>/dev/null || true"],
            build_command="npm run build --if-present",
            test_command="npm test",
            lint_command="npx --no-install eslint .",
            typecheck_command="npx --no-install tsc --noEmit",
            sast_command="npm audit --json",
            extra_whitelist=[
                "node", "npm", "npx", "tsc", "eslint", "semgrep", "ls", "cat",
            ],
        )
    if is_lang("go", "golang", " go "):
        return TaskHarness(
            language="go",
            setup_commands=["go mod download 2>/dev/null || true"],
            build_command="go build ./...",
            test_command="go test ./...",
            lint_command="go vet ./...",
            sast_command="gosec -fmt=json ./... 2>/dev/null || govulncheck ./...",
            extra_whitelist=[
                "go ", "gofmt", "go vet", "staticcheck", "gosec", "govulncheck",
                "ls", "cat",
            ],
        )
    if is_lang("rust", "rust", "cargo"):
        return TaskHarness(
            language="rust",
            build_command="cargo build",
            test_command="cargo test",
            lint_command="cargo clippy -- -D warnings",
            sast_command="cargo audit",
            extra_whitelist=[
                "cargo", "rustc", "clippy-driver", "cargo-audit", "ls", "cat",
            ],
        )
    if is_lang("java", "maven", "gradle", "java", "spring"):
        return TaskHarness(
            language="java",
            build_command="mvn -q compile",
            test_command="mvn -q test",
            lint_command="mvn -q checkstyle:check",
            sast_command="mvn -q com.github.spotbugs:spotbugs-maven-plugin:check",
            extra_whitelist=[
                "mvn", "gradle", "javac", "checkstyle", "spotbugs",
                "dependency-check", "ls", "cat",
            ],
        )
    # 兜底：通用，至少放行基本探查命令 + 跨语言密钥扫描
    return TaskHarness(
        language="",
        extra_whitelist=["ls", "cat", "python -c", "python -m py_compile", "gitleaks", "trufflehog"],
    )


def _infer_intent(task_description: str, *, greenfield: bool = False) -> "TaskIntent":
    """从任务描述启发式推断意图（LLM 未显式给出时的兜底）。

    优先级：审计/排错/重构 关键词 > greenfield(新建) > 默认 MODIFY。
    LLM plan 显式给出 intent 时以 LLM 为准（TaskPlan(**result) 自动覆盖）。
    """
    from swarm.types import TaskIntent

    t = (task_description or "").lower()

    def has(*kw: str) -> bool:
        return any(k in t for k in kw)

    if has("安全审计", "审计", "漏洞", "security audit", "audit", "sast",
           "vulnerab", "cve", "渗透", "安全扫描", "密钥泄露", "secret scan"):
        return TaskIntent.AUDIT
    if has("排错", "调试", "修复 bug", "fix bug", "debug", "报错", "复现",
           "traceback", "异常", "崩溃", "stack trace", "failing test"):
        return TaskIntent.DEBUG
    if has("重构", "refactor", "重组", "拆分模块", "解耦", "整理代码", "代码清理"):
        return TaskIntent.REFACTOR
    if greenfield or has("从零", "新建项目", "写一个", "写个", "实现一个", "做一个",
                         "create a new", "build a", "greenfield", "scaffold"):
        return TaskIntent.CREATE
    return TaskIntent.MODIFY


def _match_files_by_description(task_description: str, candidate_files: list[str]) -> list[str]:
    """用描述中的标识符 token(类名/文件名)精确匹配候选文件 basename。

    解决"未显式写文件路径但点了类名"的场景(如 "给 StringUtils 加方法")：从描述
    提取标识符 token，匹配检索候选的 basename(不含扩展名)，命中才作为 writable
    目标，避免把整个检索候选集设为可写。返回匹配到的文件路径(去重保序)。
    """
    import re as _re

    if not task_description or not candidate_files:
        return []
    tokens = {t.lower() for t in _re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task_description)}
    if not tokens:
        return []
    matched: list[str] = []
    for fp in candidate_files:
        stem = fp.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        if stem in tokens:  # basename(无扩展)整体命中，避免泛匹配整个模块
            matched.append(fp)
    return list(dict.fromkeys(matched))


def _build_simple_plan(task_description: str, affected_files: list[str] | None = None) -> TaskPlan:
    # Scope 解析优先级（确保 scope 既非空又精准）：
    # 1) 任务描述中【显式点名】的文件（如 "只修改 README.md"）—— 最强信号，
    #    用户意图明确时，writable 应严格限定为这些文件，避免改到无关文件。
    # 2) analyze 节点经知识库检索得到的 affected_files —— 作为上下文/回退。
    ops = _classify_file_ops(task_description)
    explicit = ops["modify"] + ops["create"] + ops["delete"]
    retrieved = [f for f in (affected_files or []) if f]

    if explicit:
        # 用户点名了文件：按操作意图分别填 modify/create/delete；
        # readable 额外纳入检索文件作上下文。
        # ── 关键修复(task ec2b095b)：两个语义陷阱 ──
        # ① _classify_file_ops 把"给已有类【新增】方法"误判为 create file（"新增"关键词），
        #    但给已存在文件加方法实质是 modify，不是创建新文件。
        # ② 点名常是【裸文件名】(如 "StringUtils.java")，项目真实路径是
        #    "ruoyi-common/src/.../StringUtils.java"。裸名直接进 scope → bootstrap 在
        #    /workspace 找不到(uploaded=0,errors=1) → worker 拿不存在的文件无从下手 →
        #    绕圈耗尽步数 → "Sorry, need more steps"（与模型无关，换 40B-Claude 也一样）。
        # 解法：对每个点名文件，先在 retrieved(已存在文件全路径集)里按 basename 匹配——
        #   命中(文件已存在) → 解析成全路径 + 强制归 modify（无论原判 modify/create）；
        #   未命中 → 保留原操作类型(可能是真新建/真删除)与裸名。
        def _resolve_one(n: str) -> tuple[str, bool]:
            """返回 (解析后路径, 是否已存在于项目)。"""
            if "/" in n or "\\" in n:
                exists = any(r == n for r in retrieved)
                return n, exists
            matches = [r for r in retrieved if r.rsplit("/", 1)[-1] == n]
            if matches:
                return (matches[0] if len(matches) == 1 else sorted(matches, key=len)[0]), True
            return n, False

        modify_files: list[str] = []
        create_files: list[str] = []
        delete_files: list[str] = []
        for n in dict.fromkeys(ops["delete"]):
            path, exists = _resolve_one(n)
            delete_files.append(path)  # 删除意图明确，保留
        for n in dict.fromkeys(ops["modify"] + ops["create"]):
            path, exists = _resolve_one(n)
            if exists:
                modify_files.append(path)   # 文件已存在 → 一律 modify（修正 create 误判）
            elif n in ops["create"]:
                create_files.append(path)   # 项目里没有 + 原判 create → 真新建
            else:
                modify_files.append(path)   # 原判 modify 但没检索到 → 仍按 modify 试
        modify_files = list(dict.fromkeys(modify_files))
        create_files = list(dict.fromkeys(create_files))
        delete_files = list(dict.fromkeys(delete_files))
        readable = list(dict.fromkeys(modify_files + create_files + delete_files + retrieved))
    elif retrieved:
        # 未显式点名文件，但检索到候选。【不要把所有检索文件一股脑塞进 writable】——
        # 那会导致 worker 上传/拉回整个模块、diff 巨大且脏(实测 RuoYi "加一个方法"
        # 却圈了 88 文件)。改为：用描述里的【标识符 token】(类名/文件名)精确匹配检索
        # 文件的 basename，命中的才作为 writable 目标；全部检索文件作 readable 上下文。
        name_matched = _match_files_by_description(task_description, retrieved)
        if name_matched:
            modify_files = name_matched
        else:
            # 没匹配到具体文件：writable 留空 + allow_any，让 worker 在检索上下文里
            # 自行定位并创建/修改目标文件，而不是盲目把整个候选集设为可写。
            modify_files = []
        create_files = []
        delete_files = []
        readable = list(dict.fromkeys(retrieved))
    else:
        modify_files = []
        create_files = []
        delete_files = []
        readable = []
    # 无明确写目标（开放式需求 或 检索未精确命中文件）→ 放行任意路径，
    # 否则 scope_guard 会拒绝所有写操作导致 worker 寸步难行。
    allow_any = not (modify_files or create_files or delete_files)
    scope = FileScope(
        readable=readable,
        writable=modify_files,
        create_files=create_files,
        delete_files=delete_files,
        allow_any=allow_any,
    )
    return TaskPlan(
        subtasks=[
            SubTask(
                id="st-1",
                description=task_description,
                intent=_infer_intent(task_description, greenfield=allow_any),
                difficulty=SubTaskDifficulty.TRIVIAL,
                modality=SubTaskModality.TEXT,
                scope=scope,
                contract={"input": "当前代码", "output": "按要求修改后的代码"},
                acceptance_criteria=["变更符合任务描述", "语法检查通过"],
                depends_on=[],
                harness=_infer_harness(task_description, scope),
            )
        ],
        parallel_groups=[["st-1"]],
    )


def _complexity_str(complexity: Complexity | str | None) -> str:
    if complexity is None:
        return Complexity.MEDIUM.value
    if hasattr(complexity, "value"):
        return complexity.value
    return str(complexity)


def _parse_json_from_llm(text: str | list) -> dict:
    """从 LLM 输出中解析 JSON（支持 markdown 代码块）

    Args:
        text: LLM response.content，可能是 str 或 list (多模态消息)
    """
    # 处理多模态 content（list 类型）
    if isinstance(text, list):
        # 提取文本部分
        parts = [item for item in text if isinstance(item, str)]
        if not parts:
            parts = [item.get("text", "") for item in text if isinstance(item, dict) and "text" in item]
        text = "\n".join(parts)
    # 去除 markdown 代码块包裹
    text = text.strip()
    if text.startswith("```"):
        # audit #18：边界保护——` ```json ` 无换行时 index 会 ValueError；rfind 找不到
        # 收尾 ``` 时返回 -1 导致截取范围出错。改为安全提取。
        first_nl = text.find("\n")  # find 而非 index：无换行返回 -1 不抛
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            text = text[first_nl + 1 : last_fence].strip()
        else:
            # 退化形态：剥掉开头的 ```lang 与可能的收尾 ```，尽力取中间内容
            body = text[3:]
            if body.endswith("```"):
                body = body[:-3]
            # 去掉紧随 ``` 的语言标识首行（若存在）
            nl = body.find("\n")
            if nl != -1 and " " not in body[:nl].strip():
                body = body[nl + 1 :]
            text = body.strip()
    return json.loads(text)


def _brain_profile_prompt(state: BrainState) -> str:
    return state.get("user_profile_prompt_brain") or "（未加载用户画像）"


def _worker_profile_prompt(state: BrainState) -> str:
    return state.get("user_profile_prompt_worker") or "（未加载用户画像）"


def _planning_triage(task_description: str, complexity: Complexity, state: BrainState) -> dict:
    """规划初判（Q4）：是否微任务 + 是否需进澄清流程。不额外调 LLM（控成本）。

    - 微任务：启发式判 simple 且描述短、含典型微改动词（改/换/调/重命名/文案/颜色）→ 极速通道。
    - needs_clarify：非微任务 且 非自动化模式 → 倾向进澄清（真复杂度由 assess 澄清后定）。
    """
    desc = (task_description or "").strip()
    micro_markers = ("改", "换成", "调整", "重命名", "文案", "颜色", "typo", "错别字", "rename", "颜色", "样式")
    is_micro = (
        complexity == Complexity.SIMPLE
        and len(desc) <= 40
        and any(m in desc.lower() for m in micro_markers)
    )
    auto = bool(state.get("auto_accept")) or os.environ.get("SWARM_AUTO_ACCEPT", "").lower() in ("1", "true", "yes")
    needs_clarify = (not is_micro) and (not auto)
    return {"is_micro_task": is_micro, "needs_clarify": needs_clarify}


_L2_CMD_RE = re.compile(
    r"\b((?:pytest|python\s+-m\s+pytest|npm\s+test|mvn\s+test|make\s+test)(?:\s+[^\n;|]+)?)",
    re.IGNORECASE,
)


def _l2_test_command_from_criteria(criteria: list[str]) -> str:
    for item in criteria:
        match = _L2_CMD_RE.search(item)
        if match:
            return match.group(1).strip()
        stripped = item.strip()
        if stripped.startswith(("pytest", "python -m pytest", "npm test", "mvn test", "make test")):
            return stripped
    return "pytest -q"


# B1 批3: verify_l2 引用的纯函数，归入 shared
def _diff_has_changes(diff: str) -> bool:
    return any(
        line.startswith("+") and not line.startswith("+++")
        for line in (diff or "").splitlines()
    )
