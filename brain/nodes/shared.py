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


def parse_marker_rc(output: str | None, marker: str = "__RC__") -> int | None:
    """从沙箱命令输出解析【末尾】echo 出的退出码标记 `<marker><rc>`。

    B3-F6 治本：旧口径 `"<marker>0" in out` 是无锚点全文子串匹配——构建/测试日志任意位置
    出现该字面串（测试名/路径/被测代码回显 marker）即假成功/假失败，与真退出码脱钩。
    这里取【最后一次】匹配的捕获组（`echo <marker>$?` 的输出恒在合并 stdout+stderr 末尾），
    与既有正确兄弟 migration_verify.py:477 `re.findall(r"__RC__(-?\\d+)", out)[-1]` 同口径。

    返回 int 退出码；marker 从未出现 → None（命令没跑成=infra 中断，调用方据此降级，
    绝不把 infra 判成测试/编译失败，也绝不据无锚点子串判成功）。
    """
    if not output:
        return None
    ms = re.findall(rf"{re.escape(marker)}(-?\d+)", output)
    if not ms:
        return None
    try:
        return int(ms[-1])
    except (TypeError, ValueError):  # pragma: no cover — 正则已限定 -?\d+
        return None


def l1_passed(out) -> bool:
    """子任务输出 L1 是否通过 —— 单一事实源（round24 A1，替代 4 处副本）。

    形态：WorkerOutput 或 dict 或 None。鸭子判超集（任意带 l1_passed 属性的对象
    都识别，非仅 WorkerOutput 实例），对真实输入（WorkerOutput/dict/None）与原 4 处
    副本等价。行为契约见 test/test_l1_verdict.py。
    """
    v = getattr(out, "l1_passed", None)
    if v is None and isinstance(out, dict):
        v = out.get("l1_passed")
    return bool(v)


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
            test_command="",  # S1(task 34fab09e)：默认不带测试命令，任务明确要求测试时才由规划注入
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
            test_command="",  # S1：默认不带测试命令
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
            test_command="",  # S1：默认不带测试命令
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
            test_command="",  # S1：默认不带测试命令
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
            test_command="",  # S1：默认不带测试命令（RuoYi 等项目常无测试依赖，强跑必失败）
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


def _build_simple_plan(
    task_description: str,
    affected_files: list[str] | None = None,
    project_path: str | None = None,
) -> TaskPlan:
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
        # ── 根因修复(task 9bd1d5b5)：retrieved 来自【知识库检索】，索引滞后——
        #   前一个任务刚 commit 的文件(如 monitor/HealthController.java)还没被索引，
        #   retrieved 里查不到 → basename 匹配失败 → 退回用 LLM 猜的裸名/错路径(common/) →
        #   bootstrap 找不到 → worker 拿空文件绕圈 → "Sorry, need more steps" 拒答失败。
        #   治本：除 retrieved 外，再查 git ls-files + 工作区磁盘(ground truth，不滞后)，
        #   建 basename→真实路径 索引兜底。事实库不滞后的关键是【定位用 ground truth 不靠索引】。
        ground_truth: dict[str, list[str]] = {}
        if project_path:
            import os as _os
            import subprocess as _sp
            try:
                _r = _sp.run(["git", "-C", project_path, "ls-files"],
                             capture_output=True, text=True, timeout=20)
                if _r.returncode == 0:
                    for _p in _r.stdout.splitlines():
                        _p = _p.strip()
                        if _p:
                            ground_truth.setdefault(_os.path.basename(_p).lower(), []).append(_p)
            except Exception:  # noqa: BLE001
                pass
            # 磁盘补充（未跟踪但已存在的文件）
            try:
                for _root, _dirs, _files in _os.walk(project_path):
                    _dirs[:] = [d for d in _dirs if d not in (
                        ".git", "node_modules", "target", "dist", "build", ".venv",
                        "__pycache__", ".idea", ".codegraph")]
                    for _f in _files:
                        _rel = _os.path.relpath(_os.path.join(_root, _f), project_path)
                        ground_truth.setdefault(_f.lower(), [])
                        if _rel not in ground_truth[_f.lower()]:
                            ground_truth[_f.lower()].append(_rel)
            except Exception:  # noqa: BLE001
                pass

        def _resolve_one(n: str) -> tuple[str, bool]:
            """返回 (解析后路径, 是否已存在于项目)。retrieved 优先，ground truth 兜底。"""
            base_n = n.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            if "/" in n or "\\" in n:
                # 带路径：先精确匹配 retrieved
                if any(r == n for r in retrieved):
                    return n, True
                # retrieved 没有 → 查 ground truth 的 basename（路径可能是 LLM 猜错的目录）
                gt = ground_truth.get(base_n.lower(), [])
                if gt:
                    # 真实路径覆盖 LLM 猜的路径（事实优先）
                    return (gt[0] if len(gt) == 1 else sorted(gt, key=len)[0]), True
                return n, False
            # 裸名：retrieved basename 匹配
            matches = [r for r in retrieved if r.rsplit("/", 1)[-1] == n]
            if matches:
                return (matches[0] if len(matches) == 1 else sorted(matches, key=len)[0]), True
            # retrieved 没有 → ground truth 兜底
            gt = ground_truth.get(n.lower(), [])
            if gt:
                return (gt[0] if len(gt) == 1 else sorted(gt, key=len)[0]), True
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
    # 严格解析优先（合法 JSON 零行为差）；失败则 json_repair 修复 LLM 常见瑕疵
    # （缺逗号/尾逗号/截断/Python 字面量 True/None…）。治本 RUN10：TECH_DESIGN stage1 因
    # 单个 'Expecting , delimiter' 整个设计塌成空方案 → PLAN 凭空拼小计划 → 欠 PRD 40-50% 假 DONE。
    # 一处鲁棒化保护所有 LLM JSON 解析点(tech_design 两阶段/模块批/plan 分批/单发)。
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError) as _je:
        try:
            import json_repair
        except ImportError:
            raise _je  # 无修复库则维持原异常，交调用方重试/降级
        repaired = json_repair.loads(text)
        if not isinstance(repaired, (dict, list)) or repaired == "" or repaired == []:
            raise _je  # 修复后仍非结构化/空 → 维持原异常，不静默吞成空
        import logging
        logging.getLogger(__name__).warning(
            "[_parse_json] 严格 JSON 解析失败(%s)，json_repair 修复成功", str(_je)[:60])
        return repaired


def parse_and_validate(text: str | list, model_cls):
    """解析 LLM JSON 并按 Pydantic 模型校验（Wave 1 / TD2606-B1 的类型边界）。

    成功 → 返回校验后的【模型实例】（载荷关键字段已类型化）。
    失败（JSON 解析失败 / 形状非法 / 缺载荷关键字段）→ 抛异常，由调用方【显式降级/重试】，
    绝不静默返回错形 dict 让坏数据流向下游。

    Args:
        text: LLM response.content（str 或多模态 list）。
        model_cls: pydantic BaseModel 子类（见 brain/llm_schemas.py）。
    """
    data = _parse_json_from_llm(text)
    return model_cls.model_validate(data)


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
    # 仅当 acceptance_criteria 里【显式包含】测试命令时才返回它；否则返回空串。
    # 旧逻辑默认返回 "pytest -q"（写死 Python），对 Java/Go/前端等非 Python 项目必然失败，
    # 且任务【未要求测试】时本就不该跑测试——VERIFY_L2 的 integration_review（编译+契约+
    # git apply 同源）已是充分的确定性集成验证。返回空串 → 上层跳过沙箱/本地测试验证，
    # 不因写死框架或无谓测试而误判 L2 失败（task dc1ec890：Java 项目被 pytest -q 卡死）。
    for item in criteria:
        match = _L2_CMD_RE.search(item)
        if match:
            return match.group(1).strip()
        stripped = item.strip()
        if stripped.startswith(("pytest", "python -m pytest", "npm test", "mvn test", "make test")):
            return stripped
    return ""


# B1 批3: verify_l2 引用的纯函数，归入 shared
def _diff_has_changes(diff: str) -> bool:
    return any(
        line.startswith("+") and not line.startswith("+++")
        for line in (diff or "").splitlines()
    )


def _diff_has_deletions(diff: str) -> bool:
    """diff 是否表达【删除】——纯删除子任务的产出形态。

    删除补丁只有 `-` 内容行（不含 `---` 文件头）与 `+++ /dev/null` 哨兵，无 `+` 内容行，
    故 `_diff_has_changes` 恒 False。这里以删除侧信号判定"确有删除产物"。栈/后缀无关。
    """
    for line in (diff or "").splitlines():
        if line.strip() == "+++ /dev/null":
            return True
        if line.startswith("-") and not line.startswith("---"):
            return True
    return False


def _is_pure_delete_scope(scope) -> bool:
    """子任务 scope 是否【纯删除】：只声明 delete_files，无 writable/create_files/allow_any。

    纯删除子任务的合法产出是"删除段"（无 `+` 行）。据 scope 意图判定，绝不硬编码语言/后缀/项目。
    """
    if scope is None:
        return False
    return (
        bool(getattr(scope, "delete_files", None))
        and not getattr(scope, "writable", None)
        and not getattr(scope, "create_files", None)
        and not getattr(scope, "allow_any", False)
    )


def _subtask_produced_expected(worker_output, subtask) -> bool:
    """子任务是否产出了【该 intent/scope 下预期的变更形态】——dispatch 成功判据的一半。

    治本 D01：旧判据 `_diff_has_changes`（只认 `+` 行）对两类合法产出结构性误判失败——
      · AUDIT 意图：产结构化审计报告而非 diff，空 diff 合法（交付有效性由 l1_passed 表达）；
      · 纯删除子任务：diff 只有 `-` 行 + `+++ /dev/null`，无 `+` 行。
    这里按【预期变更类型】而非"存在 + 行"判定。栈/后缀/项目无关，fail-closed：
    非 AUDIT、非纯删除的普通子任务仍要求有真实 `+` 变更（空产出=失败）。

    注：本函数只回答"形态是否符合预期"，是否通过质量闸门仍由调用侧的 l1_passed 独立把关。
    """
    from swarm.types import TaskIntent
    if getattr(subtask, "intent", None) == TaskIntent.AUDIT:
        # AUDIT 不产 diff；空 diff 是预期形态，有效性交由 l1_passed（should_block 反转）判定
        return True
    scope = getattr(subtask, "scope", None)
    # #74 复核整改：worker_output 可能是 WorkerOutput 或【dict】（checkpoint 反序列化形态）——两者都取 diff，
    # 否则 dict 结果 getattr 恒 None → 普通子任务被误判"未产出"。
    diff = (getattr(worker_output, "diff", None)
            or (worker_output.get("diff") if isinstance(worker_output, dict) else None) or "")
    if _is_pure_delete_scope(scope):
        # 纯删除：删除段即有效产出；若同时含新增行（罕见）也算有产出
        return _diff_has_deletions(diff) or _diff_has_changes(diff)
    return _diff_has_changes(diff)


def completed_l1_ids(subtask_results: dict) -> set:
    """L1 通过的已完成子任务 ID 集——依赖闸门的单一事实源（治本 D23）。

    旧口径 `set(subtask_results.keys())` 把【L1 未通过的滞留失败结果】也当"已完成"，下游据此
    误判依赖满足而提前派发（上游从未真正成功）→ BLOCKED/编译失败空烧。这里只计 l1_passed 为真者，
    消费 WorkerOutput.l1_passed 单一事实源（见本模块 l1_passed()）。
    """
    return {sid for sid, out in (subtask_results or {}).items() if l1_passed(out)}


def _is_test_file_path(p: str) -> bool:
    """判定是否测试文件路径（跨语言：java/py/js/ts/go）。"""
    pl = str(p or "").replace("\\", "/").lower()
    base = pl.rsplit("/", 1)[-1]
    return (
        "/test/" in pl or "/tests/" in pl or "/src/test/" in pl
        or base.endswith("test.java") or base.endswith("tests.java")
        or base.startswith("test_") or base.endswith("_test.py")
        or base.endswith(".test.js") or base.endswith(".test.ts")
        or base.endswith(".spec.ts") or base.endswith(".spec.js")
        or base.endswith("_test.go")
    )


def _task_requests_tests(task_description: str) -> bool:
    """任务【是否明确要求】写/跑测试。保守：仅在描述显式提到测试相关词时为 True。"""
    d = (task_description or "")
    return any(kw in d for kw in (
        "写测试", "单元测试", "单测", "测试用例", "测试覆盖", "加测试", "补测试",
        "unit test", "unit-test", "write test", "add test", "test coverage",
        "覆盖率",
    ))


def bootstrap_subtask_harness(st, task_description: str) -> None:
    """H1（主题H·测试门复活）：为子任务补齐 harness，就地修改 st.harness。

    PLAN_USER 让 LLM【只出 harness.verify_commands】（验收断言），build/test/lint 工具链由
    系统按语言推断。旧判据把"只有 verify_commands"的 harness 当完整→跳过推断→丢掉编译门
    （回归：verify 门在、编译门无）。此处不看 verify：只要缺 build/test/whitelist 就推断
    工具链，再把 LLM 的 verify_commands 叠加回去（推断 harness 默认 verify_commands 为空，
    叠加去重不覆盖）。LLM 若给了完整 harness（batch 路径）则原样尊重。"""
    h = getattr(st, "harness", None)
    _llm_verify = list(getattr(h, "verify_commands", []) or []) if h is not None else []
    if h is None or not (h.build_command or h.test_command or h.extra_whitelist):
        st.harness = _infer_harness(st.description or task_description, st.scope)
        if _llm_verify:
            _base_v = list(getattr(st.harness, "verify_commands", []) or [])
            st.harness.verify_commands = list(dict.fromkeys([*_base_v, *_llm_verify]))


def _strip_unrequested_tests(plan: TaskPlan, task_description: str) -> TaskPlan:
    """源头剔除未被要求的测试（task 744316e7 根因·单一事实源）。

    病根链（实测 RuoYi 两方法任务）：
      ① Brain 给"加方法"任务擅自塞测试文件进 scope.create_files（StringUtilsTest.java 等）；
      ② harness 自动带 test_command（mvn test -Dtest=XxxTest）；
      ③ worker 现造的测试用 JUnit5/4，但 ruoyi-common pom.xml 无 junit 依赖 →
         测试类【编译失败】(package org.junit does not exist) → L1 test_ok=False；
      ④ 结果：mvn compile（主代码）exit=0 实现正确，却因 mvn test 编译失败被 L1 判死，
         worker 还在修 junit 依赖上撞迭代上限(50)绕圈。
    根因：任务【没要求测试】，就不该有测试文件 + 不该有 test_command 强制门。
    本函数在 PLAN 阶段(守卫前)统一剔除：
      - scope.create_files / writable / readable 里的测试文件路径；
      - harness.test_command（消除强制测试门，只留 build_command 编译门）。
    仅当任务【未明确要求测试】时生效（_task_requests_tests=False）。worker 端 scope 兜底
    保留作二道防线，但此处是单一事实源（判定用原始 task_description，比 worker 子任务描述准）。
    """
    if _task_requests_tests(task_description):
        return plan  # 任务确实要求测试 → 保留
    subs = list(plan.subtasks or [])
    if not subs:
        return plan
    changed = False
    for st in subs:
        sc = getattr(st, "scope", None)
        if sc is not None:
            for attr in ("create_files", "writable", "readable"):
                lst = getattr(sc, attr, None) or []
                kept = [f for f in lst if not _is_test_file_path(f)]
                if len(kept) != len(lst):
                    setattr(sc, attr, kept)
                    changed = True
        # 剔除 harness 的 test_command（强制测试门）
        h = getattr(st, "harness", None)
        if h is not None and getattr(h, "test_command", ""):
            try:
                h.test_command = ""
                changed = True
            except Exception:  # noqa: BLE001
                pass
    if changed:
        import logging
        logging.getLogger(__name__).info(
            "[PLAN] 测试剔除：任务未要求测试，已从 scope 移除测试文件 + 清空 harness.test_command"
            "（防 Brain 擅自塞测试导致 L1 因 junit 缺失误判）"
        )
    return plan


def _merge_horizontal_subtasks(plan: TaskPlan) -> TaskPlan:
    """垂直切片守卫（确定性硬兜底，方向A）：合并被水平切分的同语言子任务。

    病根（task 5c17c464/94334785）：PLAN 倾向把同语言的功能按文件/按层水平切成多个子任务
    （st-1改文件A、st-2改文件B / st-1实现、st-2测试），违反"垂直功能切片"原则，制造子任务
    间依赖、MERGE 冲突、失败面放大。

    本函数把【可安全合并】的子任务合并为一个：
      - 同一沙箱语言（harness.language，决定沙箱镜像，不同语言不能合一个沙箱）；
      - 同一 modality（multimodal 看图任务不参与合并，保持隔离）；
      - 整组内部【无 depends_on 依赖】也【不被组外子任务依赖】（有依赖=真串行，尊重不动）。
    合并策略：scope 各列表并集去重、description 编号拼接、acceptance_criteria 并集、
    difficulty 取最高、intent 取多数/第一个。保守：单子任务或无可合并组时原样返回。

    注意：这【不是】把所有东西塞一个子任务——真跨语言、真有依赖、multimodal 仍各自独立。
    只消除"同语言无依赖却被拆开"这一类水平切分。
    """
    subs = list(plan.subtasks or [])
    if len(subs) <= 1:
        return plan

    def _lang(st) -> str:
        h = getattr(st, "harness", None)
        return (getattr(h, "language", "") or "").strip().lower() if h else ""

    def _modality(st) -> str:
        m = getattr(st, "modality", None)
        if m is None:
            return "text"
        return m.value if hasattr(m, "value") else str(m)

    # 任何子任务被别人依赖 or 自己依赖别人 → 整个 plan 存在真实依赖链，保守不合并
    # （依赖是 Brain 显式声明的串行需求，垂直切片守卫只处理"本可并行却被拆开"的情形）。
    has_any_dep = any(getattr(st, "depends_on", None) for st in subs)
    if has_any_dep:
        return plan

    # multimodal 子任务隔离：不参与合并
    mergeable = [st for st in subs if _modality(st) != "multimodal"]
    isolated = [st for st in subs if _modality(st) == "multimodal"]
    if len(mergeable) <= 1:
        return plan

    # 按语言分组（空语言归一组，视为同沙箱默认镜像）
    from collections import OrderedDict
    groups: "OrderedDict[str, list]" = OrderedDict()
    for st in mergeable:
        groups.setdefault(_lang(st), []).append(st)

    # 若分组后没有任何组 size>1，无可合并，原样返回
    if all(len(g) <= 1 for g in groups.values()):
        return plan

    _DIFF_RANK = {"trivial": 0, "medium": 1, "complex": 2}
    merged_subs: list = []
    _idx = 0
    for lang, group in groups.items():
        if len(group) == 1:
            merged_subs.append(group[0])
            continue
        # 合并这一组
        _idx += 1
        base = group[0]
        # scope 并集去重
        def _u(attr: str) -> list[str]:
            seen: list[str] = []
            for st in group:
                for f in (getattr(st.scope, attr, []) or []):
                    if f and f not in seen:
                        seen.append(f)
            return seen
        merged_scope = FileScope(
            writable=_u("writable"),
            readable=_u("readable"),
            create_files=_u("create_files"),
            delete_files=_u("delete_files"),
            allow_any=any(getattr(st.scope, "allow_any", False) for st in group),
        )
        # description 编号拼接（保留每个原子功能的描述）
        descs = [f"({i+1}) {st.description}" for i, st in enumerate(group)]
        merged_desc = "本子任务包含以下同语言独立功能，请在一次执行中全部完成：\n" + "\n".join(descs)
        # acceptance_criteria 并集
        ac: list[str] = []
        for st in group:
            for c in (getattr(st, "acceptance_criteria", []) or []):
                if c and c not in ac:
                    ac.append(c)
        # S2-3（task#24 必改点）：covers 并集去重——需求条目覆盖声明绝不因水平合并丢失。
        # 丢了会让 validate_plan 的覆盖矩阵把已实现的条目误判"未覆盖"→白烧 plan 重试预算。
        cov: list[str] = []
        for st in group:
            for r in (getattr(st, "covers", []) or []):
                if r and r not in cov:
                    cov.append(r)
        # difficulty 取最高
        hardest = max(
            group,
            key=lambda s: _DIFF_RANK.get(
                s.difficulty.value if hasattr(s.difficulty, "value") else str(s.difficulty), 1
            ),
        ).difficulty
        merged = SubTask(
            id=base.id,
            description=merged_desc,
            intent=base.intent,
            difficulty=hardest,
            modality=base.modality,
            scope=merged_scope,
            contract=base.contract or {},
            acceptance_criteria=ac,
            covers=cov,
            depends_on=[],
            harness=base.harness,
        )
        merged_subs.append(merged)

    merged_subs.extend(isolated)

    if len(merged_subs) == len(subs):
        return plan  # 没有实际合并发生

    import logging
    logging.getLogger(__name__).info(
        "[PLAN] 垂直切片守卫：%d 个子任务合并为 %d 个（消除同语言水平切分）",
        len(subs), len(merged_subs),
    )
    # 重建 parallel_groups：合并后各子任务独立（无依赖），各成一组
    new_ids = [st.id for st in merged_subs]
    return TaskPlan(
        subtasks=merged_subs,
        parallel_groups=[[i] for i in new_ids],
        shared_contract=getattr(plan, "shared_contract", None) or {},
    )


# ── TD2606-B8：L2 集成失败归因（把失败定位到具体子任务，避免连坐全量 replan）──
def build_writers_by_file(plan) -> dict[str, list[str]]:
    """反转每个子任务的写权（create_files ∪ writable）→ {文件相对路径: [写者子任务 id]}。

    与 contract_utils 的同名内联逻辑同源（单一事实源：scope 写权）。供 L2 失败归因把
    编译出错的文件映射回拥有它的子任务。"""
    writers: dict[str, list[str]] = {}
    for st in getattr(plan, "subtasks", []) or []:
        scope = getattr(st, "scope", None)
        if scope is None:
            continue
        files = list(getattr(scope, "create_files", []) or []) + list(
            getattr(scope, "writable", []) or []
        )
        sid = getattr(st, "id", "")
        for f in files:
            f = str(f).strip()
            if not f or not sid:
                continue
            ids = writers.setdefault(f, [])
            if sid not in ids:
                ids.append(sid)
    return writers


def attribute_l2_failure(plan, l2_details: dict | None, subtask_results: dict) -> list[str] | None:
    """把 L2 集成失败归因到具体子任务 id 列表。无法可靠定位时返回 None（调用方回退全量 replan）。

    证据来自 integration_review 的 compile_output + issues（已含出错文件路径）。对每个写权
    文件，若其相对路径或 basename 出现在证据文本里 → 该文件的写者子任务判为失败源。
    仅当结果非空【且为成功子任务集合的真子集】（即至少保留一个兄弟）时返回，否则 None
    —— 退化为全量 replan 与现状一致，绝不因误归因而把本应保留的成功成果也连坐重做。"""
    if not plan or not subtask_results:
        return None
    ir = (l2_details or {}).get("integration_review") or {}
    blob = str(ir.get("compile_output") or "")
    for it in (l2_details or {}).get("issues") or []:
        blob += "\n" + str(it)
    if not blob.strip():
        return None
    writers = build_writers_by_file(plan)
    if not writers:
        return None
    failed: list[str] = []
    for f, ids in writers.items():
        base = os.path.basename(f)
        if (f and f in blob) or (base and base in blob):
            for sid in ids:
                if sid in subtask_results and sid not in failed:
                    failed.append(sid)
    all_ids = set(subtask_results.keys())
    if failed and set(failed) < all_ids:
        return failed
    return None


# ── S1-6：运行时冒烟失败证据结构化 + 归因（复用 attribute_l2_failure 机制，勿复制）──
def runtime_failure_evidence(details: dict | None) -> str:
    """从 runtime_smoke_details 提取归因证据文本（纯函数，栈无关：只拼文本不解析栈帧）。

    只取【应用自身输出】面：log_tail（启动日志尾部）+ code_error_hits（三分类命中的错误
    形态）+ migration* 键族（S1-5 migration_failed 同族：SQL/migration 错误输出按键名前缀
    契约消费）+ acceptance* 键族（S2-6：验收断言失败证据同前缀契约——verify.py
    _acceptance_evidence_keys 写入 acceptance_evidence/acceptance_failures/
    acceptance_failed_count，含请求路径与响应 body 头部，路径/栈帧文件名能命中写者子任务）。
    绝不掺 derivation_evidence/sandbox 等【基础设施留痕】——它们含配置/构建
    文件路径（application.yml、rebuild_output 里的源文件名），入证据面会把这些文件的写者
    子任务【每轮】误归因成失败源。
    S2 复核 F4：classification=acceptance_failed 时【不收 log_tail】——此时探活已过、
    应用健康启动，log_tail 是纯启动日志噪声（其中打印的配置/资源文件名会把无辜写者
    每轮定向重派，与上述 infra 留痕同一误归因模式）；只收 acceptance/migration 前缀族
    + code_error_hits。其余分类（code_error/启动失败等）log_tail 照收——那是失败现场。
    """
    d = details or {}
    parts: list[str] = []
    if str(d.get("classification") or "") != "acceptance_failed":
        log_tail = str(d.get("log_tail") or "").strip()
        if log_tail:
            parts.append(log_tail)
    for hit in d.get("code_error_hits") or []:
        parts.append(str(hit))
    for key in sorted(d):
        if (key.startswith("migration") or key.startswith("acceptance")) and d[key]:
            val = d[key]
            if isinstance(val, (list, tuple)):
                parts.extend(str(v) for v in val if v)
            else:
                parts.append(str(val))
    return "\n".join(p for p in parts if p).strip()


def attribute_runtime_failure(
    plan, runtime_details: dict | None, subtask_results: dict
) -> list[str] | None:
    """运行时冒烟失败 → 写者子任务归因（S1-6）。

    复用 attribute_l2_failure 的「证据文本内文件路径/basename → scope 写权反查」机制
    （运行时 stack trace / migration 错误同样含源文件引用，路径形态匹配天然栈无关）；
    喂形＝把冒烟证据装进其 compile_output 席位。归因不出返回 None（调用方退 replan 阶梯），
    继承其「非真子集才定向」护栏——绝不因误归因连坐成功兄弟。
    """
    blob = runtime_failure_evidence(runtime_details)
    if not blob:
        return None
    return attribute_l2_failure(
        plan, {"integration_review": {"compile_output": blob}}, subtask_results)


def l1_details_of(out) -> dict:
    """统一取 worker 结果的 l1_details（WorkerOutput / dict / 其它）——CODEWALK §3.2 收敛：
    此前 recovery._det_of / failure._l1_details_of / failure·maven_repair 内联共 4 份同义实现。"""
    from swarm.types import WorkerOutput as _WO

    if isinstance(out, _WO):
        return out.l1_details or {}
    if isinstance(out, dict):
        return out.get("l1_details", {}) or {}
    return {}


async def gather_cancel_on_error(coros):
    """§九 阶段1 复核 H1b：gather 兄弟取消——默认 gather 首异常即抛但【不取消】其余在飞
    任务，TaskTokenLimitExceeded 逃逸后兄弟批/兄弟 worker 继续烧钱（每个可跑满各自超时），
    且其预留/结算落在 detach 之后形成幽灵条目。此处：任一任务抛 → 取消全部未完成兄弟并
    等其收尾（吞被取消者的 CancelledError，防 "exception was never retrieved" 噪声），
    再原样上抛首异常。语义：成功路径与裸 gather 逐字节一致。
    """
    import asyncio as _aio

    tasks = [_aio.ensure_future(c) for c in coros]
    try:
        return await _aio.gather(*tasks)
    except BaseException:
        for t in tasks:
            if not t.done():
                t.cancel()
        await _aio.gather(*tasks, return_exceptions=True)
        raise
