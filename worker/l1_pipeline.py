"""Worker L1 四级验证 — 确定性 scope / compile / lint / scoped test / LLM 自检。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from swarm.project.diff_apply import files_from_unified_diff
from swarm.types import FileScope, SubTask
from swarm.worker.output_compress import compress_tool_output

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


def _run_l1_command(command: str, project_path: str, timeout: int = 120) -> tuple[int, str]:
    """L1 命令执行器：沙箱优先(sandbox-first)。

    若有活跃沙箱上下文 → 在沙箱里跑(那里有 mvn/java/go/cargo 等工具链)，
    否则本地 subprocess。返回 (exit_code, output)。

    这是 L1 确定性闸门跑 build/test/verify 的统一入口——保证 Java/Go/Rust 等
    需要工具链的命令在沙箱里真实执行(本机通常没装这些工具链)。
    """
    sandbox = manager = None
    try:
        from swarm.tools.build_tools import get_sandbox_context
        sandbox, manager = get_sandbox_context()
    except Exception:  # noqa: BLE001
        sandbox = manager = None

    if sandbox is not None and manager is not None and hasattr(manager, "run_command"):
        # 沙箱里跑：cd 到远程工作目录
        try:
            from swarm.config.settings import get_config
            remote = get_config().sandbox.sandbox_remote_workdir
        except Exception:  # noqa: BLE001
            remote = "/workspace"
        cr = manager.run_command(sandbox, f"cd {remote} && {command}", timeout=timeout)
        out = (cr.stdout or "") + (("\n" + cr.stderr) if cr.stderr else "")
        # run_command 成功 success=True；失败时 error 形如 exit_code=N
        if cr.success:
            return 0, out
        ec = 1
        if cr.error and "exit_code=" in cr.error:
            try:
                ec = int(cr.error.split("exit_code=")[1].split()[0])
            except (ValueError, IndexError):
                ec = 1
        return ec, out + (f"\n{cr.error}" if cr.error else "")

    # 本地兜底
    try:
        proc = subprocess.run(
            _normalize_python_cmd(command), cwd=project_path, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "command timeout"
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


# 构建/测试命令 → 该命令运行所【必需的工程描述文件】。缺这些文件时命令必然失败
# (如 mvn 无 pom.xml、npm 无 package.json)，应优雅跳过而非误判为产出不合格。
_BUILD_TOOL_MANIFESTS: dict[str, tuple[str, ...]] = {
    "mvn": ("pom.xml",),
    "gradle": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "./gradlew": ("build.gradle", "build.gradle.kts", "settings.gradle"),
    "npm": ("package.json",),
    "yarn": ("package.json",),
    "pnpm": ("package.json",),
    "npx": ("package.json",),
    "go": ("go.mod",),
    "cargo": ("Cargo.toml",),
}


def _build_cmd_applicable(command: str, project_path: str) -> bool:
    """判断 build/test 命令的工具链工程文件是否存在(沙箱优先)。

    缺工程文件(mvn 无 pom / npm 无 package.json)时命令必失败，此时应跳过该闸门，
    不能把"工具不适用"误判成"产出不合格"。返回 True=可执行；False=应跳过。
    """
    tokens = command.strip().split()
    if not tokens:
        return False
    tool = tokens[0]
    manifests = _BUILD_TOOL_MANIFESTS.get(tool)
    if not manifests:
        return True  # 未知工具(如直接 python/pytest)不做工程文件校验，照常跑
    # 沙箱优先：在远程工作目录递归找工程文件
    sandbox = manager = None
    try:
        from swarm.tools.build_tools import get_sandbox_context
        sandbox, manager = get_sandbox_context()
    except Exception:  # noqa: BLE001
        sandbox = manager = None
    if sandbox is not None and manager is not None and hasattr(manager, "run_command"):
        try:
            from swarm.config.settings import get_config
            remote = get_config().sandbox.sandbox_remote_workdir
        except Exception:  # noqa: BLE001
            remote = "/workspace"
        # 任一 manifest 在 workspace 下存在即视为适用
        names = " -o ".join(f"-name {m!r}" for m in manifests)
        cr = manager.run_command(
            sandbox,
            f"find {remote} -maxdepth 3 \\( {names} \\) -print -quit 2>/dev/null | head -1",
            timeout=20,
        )
        return bool((cr.stdout or "").strip())
    # 本地兜底
    from pathlib import Path as _P
    root = _P(project_path)
    return any(any(root.rglob(m)) for m in manifests)



def _scope_match(fp: str, w: str) -> bool:
    """路径感知的 scope 匹配（audit #31 修复）。

    旧实现 `fp.endswith(w) or w.endswith(fp)` 是任意字符后缀匹配，会误放行：
    scope `main.py` 放行 `src/main.py`、scope `src/main.py` 放行 `2src/main.py` 等。
    新规则按【路径段】对齐，避免子串误判：
      1. 规范化(去 ./、统一 /)；
      2. 完全相等 → 匹配；
      3. w 以 / 结尾(目录 scope) → fp 在该目录下 → 匹配；
      4. fp 以 w 结尾且边界是路径分隔符(w 是 fp 的完整尾部路径段序列) → 匹配
         (容忍 diff 路径带仓库根前缀，如 scope 'src/a.py' 匹配 'repo/src/a.py')。
    """
    def norm(p: str) -> str:
        p = p.strip().replace("\\", "/")
        while p.startswith("./"):
            p = p[2:]
        return p.strip("/")

    f, ww = norm(fp), norm(w)
    if not f or not ww:
        return False
    if f == ww:
        return True
    # 目录 scope：w 原始以 / 结尾，或作为 f 的祖先目录段
    if f.startswith(ww + "/"):
        return True
    # fp 带额外根前缀：仅当 w 本身是【多段路径】(含 /) 时容忍根前缀对齐，
    # 避免单段 basename(如 'main.py') 尾匹配任意目录下同名文件(audit #31 核心)。
    if "/" in ww and f.endswith("/" + ww):
        return True
    return False


def _scope_violations(diff: str, scope: FileScope) -> list[str]:
    modified = files_from_unified_diff(diff)
    writable = set(scope.writable or [])
    if not writable:
        return []
    violations = []
    for fp in modified:
        if not any(_scope_match(fp, w) for w in writable):
            violations.append(fp)
    return violations


def _python_bin() -> str:
    """寻找可用的 Python 解释器。

    优先级：项目 .venv > 当前运行解释器(sys.executable) > python3 > python。
    用 sys.executable 而非裸 python3，确保拿到带项目依赖(pytest 等)的解释器，
    避免命中系统 python3(无 pytest)导致测试误判失败。
    """
    import sys
    if getattr(sys, "executable", ""):
        return sys.executable
    for name in ("python3", "python"):
        if shutil.which(name):
            return name
    return "python"  # 回退，让后续报错自然暴露


def _compile_files(project_path: str, files: list[str], *, timeout: int = 60) -> tuple[bool, str]:
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        py_bin = _python_bin()
        cmd = f"{py_bin} -m py_compile " + " ".join(f'"{f}"' for f in py_files[:20])
        try:
            proc = subprocess.run(
                cmd,
                cwd=project_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                return False, proc.stderr or proc.stdout or "py_compile failed"
        except Exception as exc:
            # audit #10：保留完整 traceback 便于诊断编译为何失败（原仅 str(exc) 丢栈）
            logger.warning("[L1.2] py_compile 执行异常: %s", exc, exc_info=True)
            return False, f"py_compile execution error: {exc}"

    js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
    if js_ts and os.path.isfile(os.path.join(project_path, "package.json")):
        try:
            proc = subprocess.run(
                "npx tsc --noEmit --pretty false",
                cwd=project_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0 and "error TS" in (proc.stdout or proc.stderr or ""):
                return False, (proc.stderr or proc.stdout or "")[:1000]
        except Exception as exc:
            # audit #11：tsc 编译失败可能掩盖真实编译错误，从 debug 升 warning（生产可见）
            logger.warning("[L1.2] tsc 编译跳过（异常）: %s", exc)

    return True, "compile ok"


# ── L1.2.5 lint 阶段 ──

def _find_ruff_bin() -> str | None:
    """查找 ruff 可执行文件，找不到返回 None。"""
    # 优先用 venv 内的 ruff
    candidates = [
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "ruff"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 系统 PATH
    found = shutil.which("ruff")
    if found:
        return found
    return None


def _find_tool(name: str) -> str | None:
    """通用工具探测（shutil.which），找不到返回 None。"""
    return shutil.which(name)


# ── 语言分派: per-linter 辅助 ──

def _lint_python(project_path: str, py_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Python: ruff check。返回 (has_error, messages, issues)。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    ruff_bin = _find_ruff_bin()
    if not ruff_bin:
        messages.append("ruff 未安装，跳过 Python lint")
        return has_error, messages, issues

    for fp in py_files[:20]:
        try:
            proc = subprocess.run(
                [ruff_bin, "check", fp, "--output-format=json"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            # ruff 退出码: 0=无问题, 1=有问题, 2=运行错误
            if proc.returncode == 2:
                messages.append(f"ruff 运行错误({fp}): {proc.stderr[:200]}")
                continue
            if proc.stdout.strip():
                try:
                    findings = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    findings = []
                for item in findings:
                    # ruff JSON: code 可能是 str("F401"/"invalid-syntax") 或旧版 dict{value}
                    raw_code = item.get("code")
                    if isinstance(raw_code, dict):
                        rule_code = raw_code.get("value", "") or ""
                    else:
                        rule_code = raw_code or ""
                    issue_entry = {
                        "file": fp,
                        "line": item.get("location", {}).get("row"),
                        "code": rule_code,
                        "message": item.get("message", ""),
                    }
                    # 优先用 ruff 自报的 severity；否则按代码前缀判定。
                    # invalid-syntax / E9(语法) / F4(导入*等致命) / F82(未定义名) 视为 error。
                    ruff_sev = (item.get("severity") or "").lower()
                    is_error = (
                        ruff_sev == "error"
                        or rule_code == "invalid-syntax"
                        or rule_code.startswith(("E9", "F4", "F82", "F7"))
                    )
                    if is_error:
                        issue_entry["severity"] = "error"
                        has_error = True
                    else:
                        issue_entry["severity"] = "warning"
                    issues.append(issue_entry)
        except subprocess.TimeoutExpired:
            messages.append(f"ruff 超时({fp})")
        except Exception as exc:
            messages.append(f"ruff 跳过({fp}): {exc}")
    return has_error, messages, issues


def _lint_js_ts(project_path: str, js_ts: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """JS/TS: eslint（有配置才跑）。返回 (has_error, messages, issues)。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    has_eslint_config = any(
        os.path.isfile(os.path.join(project_path, cfg))
        for cfg in (".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc", "eslint.config.js")
    )
    if not has_eslint_config:
        messages.append("项目无 eslint 配置，跳过 JS/TS lint")
        return has_error, messages, issues

    try:
        proc = subprocess.run(
            "npx eslint --format json " + " ".join(f'"{f}"' for f in js_ts[:20]),
            cwd=project_path,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # eslint 退出码: 0=无问题, 1=有问题, 2=运行错误
        if proc.returncode == 2:
            messages.append(f"eslint 运行错误: {proc.stderr[:200]}")
        elif proc.stdout.strip():
            try:
                eslint_results = json.loads(proc.stdout)
                for file_result in eslint_results:
                    for msg in file_result.get("messages", []):
                        sev = "error" if msg.get("severity") == 2 else "warning"
                        issues.append({
                            "file": file_result.get("filePath", ""),
                            "line": msg.get("line"),
                            "code": msg.get("ruleId", ""),
                            "message": msg.get("message", ""),
                            "severity": sev,
                        })
                        if sev == "error":
                            has_error = True
            except json.JSONDecodeError:
                messages.append("eslint 输出解析失败")
    except subprocess.TimeoutExpired:
        messages.append("eslint 超时")
    except Exception as exc:
        messages.append(f"eslint 跳过: {exc}")
    return has_error, messages, issues


def _lint_go(project_path: str, go_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Go: go vet ./...（在 project_path 跑；非0退出且有 error 输出才算 has_error）。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    go_bin = _find_tool("go")
    if not go_bin:
        messages.append("go 未安装，跳过 Go lint")
        return has_error, messages, issues

    # 无 go.mod 时 go vet 无法跑，跳过（对齐 eslint 无配置则跳过的风格）
    if not os.path.isfile(os.path.join(project_path, "go.mod")):
        messages.append("项目无 go.mod，跳过 Go lint")
        return has_error, messages, issues

    try:
        proc = subprocess.run(
            [go_bin, "vet", "./..."],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err_output = (proc.stderr or "").strip()
            if err_output:
                for line in err_output.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    issue_entry: dict = {
                        "file": "",
                        "line": None,
                        "code": "govet",
                        "message": line,
                        "severity": "error",
                    }
                    # 尝试解析 file:line:col: message 格式
                    parts = line.split(":")
                    if len(parts) >= 2:
                        issue_entry["file"] = parts[0]
                        try:
                            issue_entry["line"] = int(parts[1])
                        except ValueError:
                            pass
                    issues.append(issue_entry)
                has_error = True
    except subprocess.TimeoutExpired:
        messages.append("go vet 超时")
    except Exception as exc:
        messages.append(f"go vet 跳过: {exc}")
    return has_error, messages, issues


def _lint_rust(project_path: str, rs_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Rust: cargo clippy -- -D warnings（clippy 把 warning 当 error）。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    cargo_bin = _find_tool("cargo")
    if not cargo_bin:
        messages.append("cargo 未安装，跳过 Rust lint")
        return has_error, messages, issues

    # 无 Cargo.toml 时 cargo clippy 无法跑，跳过
    if not os.path.isfile(os.path.join(project_path, "Cargo.toml")):
        messages.append("项目无 Cargo.toml，跳过 Rust lint")
        return has_error, messages, issues

    try:
        proc = subprocess.run(
            [cargo_bin, "clippy", "--", "-D", "warnings"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err_output = (proc.stderr or "").strip()
            if err_output:
                for line in err_output.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    # 跳过摘要行
                    if line.startswith("warning: generated") or line.startswith("error: aborting"):
                        continue
                    if ": error[" in line or ": warning[" in line or line.startswith("error:"):
                        issue_entry: dict = {
                            "file": "",
                            "line": None,
                            "code": "clippy",
                            "message": line,
                            "severity": "error",  # -D warnings => all warnings are errors
                        }
                        # 尝试解析 file:line:col 格式
                        # Rust 输出: src/main.rs:2:5: error[E0425]: ...
                        for prefix in line.split(": "):
                            parts = prefix.split(":")
                            if len(parts) >= 2:
                                try:
                                    int(parts[1])
                                    issue_entry["file"] = parts[0]
                                    issue_entry["line"] = int(parts[1])
                                    break
                                except ValueError:
                                    continue
                        issues.append(issue_entry)
                if issues:
                    has_error = True
    except subprocess.TimeoutExpired:
        messages.append("cargo clippy 超时")
    except Exception as exc:
        messages.append(f"cargo clippy 跳过: {exc}")
    return has_error, messages, issues


def _lint_java(project_path: str, java_files: list[str], *, timeout: int = 60) -> tuple[bool, list[str], list[dict]]:
    """Java/Kotlin: checkstyle（找不到 checkstyle 就 skip，不报错）。"""
    has_error = False
    messages: list[str] = []
    issues: list[dict] = []

    checkstyle_bin = _find_tool("checkstyle")
    if not checkstyle_bin:
        messages.append("checkstyle 未安装，跳过 Java lint")
        return has_error, messages, issues

    try:
        proc = subprocess.run(
            [checkstyle_bin] + java_files[:20],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            err_output = (proc.stderr or proc.stdout or "").strip()
            if err_output:
                for line in err_output.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    issue_entry: dict = {
                        "file": "",
                        "line": None,
                        "code": "checkstyle",
                        "message": line,
                        "severity": "error",
                    }
                    # 尝试解析 [ERROR] file:line:col: message 格式
                    import re
                    m = re.match(r"\[(?:ERROR|WARN)\]\s+(.+?):(\d+)", line)
                    if m:
                        issue_entry["file"] = m.group(1)
                        issue_entry["line"] = int(m.group(2))
                    issues.append(issue_entry)
                has_error = True
    except subprocess.TimeoutExpired:
        messages.append("checkstyle 超时")
    except Exception as exc:
        messages.append(f"checkstyle 跳过: {exc}")
    return has_error, messages, issues


def _lint_files(project_path: str, files: list[str], *, timeout: int = 60) -> tuple[bool, str, list[dict]]:
    """对修改的文件跑 lint（按语言分派矩阵），返回 (has_error, message, issues)。

    语言分派：
    - Python (.py): ruff check
    - JS/TS (.js/.jsx/.ts/.tsx): eslint（项目有配置才跑）
    - Go (.go): go vet ./...（无 go.mod 跳过）
    - Rust (.rs): cargo clippy -- -D warnings（无 Cargo.toml 跳过）
    - Java/Kotlin (.java/.kt): checkstyle（找不到工具则跳过）
    - lint 工具不可用时优雅跳过，绝不让缺工具导致崩溃或误判失败
    """
    issues: list[dict] = []
    has_error = False
    messages: list[str] = []

    # ── 按语言分组 ──
    lang_groups: dict[str, list[str]] = {
        "python": [],
        "js_ts": [],
        "go": [],
        "rust": [],
        "java": [],
    }
    for f in files:
        if f.endswith(".py"):
            lang_groups["python"].append(f)
        elif f.endswith((".ts", ".tsx", ".js", ".jsx")):
            lang_groups["js_ts"].append(f)
        elif f.endswith(".go"):
            lang_groups["go"].append(f)
        elif f.endswith(".rs"):
            lang_groups["rust"].append(f)
        elif f.endswith((".java", ".kt")):
            lang_groups["java"].append(f)

    # ── Python: ruff check ──
    py_files = lang_groups["python"]
    if py_files:
        py_err, py_msgs, py_issues = _lint_python(project_path, py_files, timeout=timeout)
        has_error = has_error or py_err
        messages.extend(py_msgs)
        issues.extend(py_issues)

    # ── JS/TS: eslint ──
    js_ts = lang_groups["js_ts"]
    if js_ts:
        js_err, js_msgs, js_issues = _lint_js_ts(project_path, js_ts, timeout=timeout)
        has_error = has_error or js_err
        messages.extend(js_msgs)
        issues.extend(js_issues)

    # ── Go: go vet ──
    go_files = lang_groups["go"]
    if go_files:
        go_err, go_msgs, go_issues = _lint_go(project_path, go_files, timeout=timeout)
        has_error = has_error or go_err
        messages.extend(go_msgs)
        issues.extend(go_issues)

    # ── Rust: cargo clippy ──
    rs_files = lang_groups["rust"]
    if rs_files:
        rs_err, rs_msgs, rs_issues = _lint_rust(project_path, rs_files, timeout=timeout)
        has_error = has_error or rs_err
        messages.extend(rs_msgs)
        issues.extend(rs_issues)

    # ── Java/Kotlin: checkstyle ──
    java_files = lang_groups["java"]
    if java_files:
        java_err, java_msgs, java_issues = _lint_java(project_path, java_files, timeout=timeout)
        has_error = has_error or java_err
        messages.extend(java_msgs)
        issues.extend(java_issues)

    summary = "; ".join(messages) if messages else "lint ok"
    return has_error, summary, issues


# ── L1.4 LLM 自检阶段 ──

_SELF_REVIEW_PROMPT = """\
你是一位严格的代码审查员。请对以下代码变更进行自检，检查：
1. 是否完整实现了子任务目标
2. 边界情况是否处理
3. 是否违反约束（如 scope 越权、硬编码密钥等）
4. 代码风格一致性

子任务描述：
{description}

可写范围：
{writable}

变更 diff：
{diff}

请严格按照以下 JSON 格式回答（不要输出其他内容）：
{{"passed": true/false, "issues": ["问题1", "问题2"]}}
如果未发现实质性问题，passed 为 true，issues 为空列表。
"""


def _run_self_review(
    llm: BaseChatModel,
    subtask: SubTask,
    diff: str,
    *,
    timeout: int = 60,
) -> dict[str, Any]:
    """LLM 自检：调用 LLM 审查代码变更，返回 {passed, issues, raw}。"""
    prompt = _SELF_REVIEW_PROMPT.format(
        description=subtask.description,
        writable=", ".join(subtask.scope.writable or []),
        diff=diff[:4000],  # 截断避免超长
    )
    text = ""  # 预初始化避免 except 中未绑定
    try:
        from langchain_core.messages import HumanMessage
        response = llm.invoke([HumanMessage(content=prompt)])
        text = getattr(response, "content", str(response))
        # 提取 JSON（兼容 markdown 代码块包裹）
        json_str = text.strip()
        if "```" in json_str:
            # 取代码块内容
            parts = json_str.split("```")
            for p in parts:
                p = p.strip()
                if p.startswith("{"):
                    json_str = p
                    break
        # 去掉可能的语言标记
        if json_str.startswith("json"):
            json_str = json_str[4:].strip()
        result = json.loads(json_str)
        passed = bool(result.get("passed", True))
        issues = result.get("issues", [])
        if not isinstance(issues, list):
            issues = [str(issues)]
        return {"passed": passed, "issues": issues, "raw": text[:500]}
    except json.JSONDecodeError:
        logger.debug("LLM 自检输出非标准 JSON，视为通过")
        return {"passed": True, "issues": [], "raw": text[:500] or "json parse error"}
    except Exception as exc:
        logger.debug("LLM 自检异常，跳过: %s", exc)
        return {"passed": True, "issues": [], "raw": f"self_review skipped: {exc}"}


# ── 主流水线 ──

def _guess_test_cmd(project_path: str, modified: list[str]) -> str | None:
    for fp in modified:
        base = Path(fp).stem
        if fp.endswith(".py"):
            candidates = [
                f"tests/test_{base}.py",
                f"test/test_{base}.py",
                f"test_{base}.py",
            ]
            for c in candidates:
                if os.path.isfile(os.path.join(project_path, c)):
                    return f"python -m pytest -q {c}"
    if os.path.isfile(os.path.join(project_path, "pyproject.toml")):
        return "python -m pytest -q --maxfail=1"
    return None


def _normalize_python_cmd(cmd: str) -> str:
    """把命令里的裸 `python`/`python3` 归一到本机可用解释器。

    Brain/LLM 生成的 harness 常写 `python ...`，但本机(确定性闸门运行处)可能只有
    `python3`(macOS 常见)。沙箱里 python 存在，本地却 command not found —— 这类
    环境漂移会让确定性验证误判。统一替换前缀。
    """
    if not cmd:
        return cmd
    py = _python_bin()
    if py == "python":
        return cmd
    # 替换行首或 && / ; / | 后的裸 python（不动 python3 已正确的情况）
    import re
    return re.sub(r"(^|[\s;&|])python(?=\s)", lambda m: f"{m.group(1)}{py}", cmd)


def _maven_modules(project_path: str) -> dict[str, str]:
    """返回 {模块目录名: 相对路径} 映射(读根 pom 的 <module> 列表)。无则空。"""
    from pathlib import Path as _P
    root = _P(project_path)
    pom = root / "pom.xml"
    if not pom.is_file():
        return {}
    try:
        import re
        text = pom.read_text("utf-8", errors="ignore")
        mods = re.findall(r"<module>\s*([^<\s]+)\s*</module>", text)
        return {m.rstrip("/").split("/")[-1]: m.rstrip("/") for m in mods}
    except Exception:  # noqa: BLE001
        return {}


def _scope_maven_command(command: str, project_path: str, modified: list[str]) -> str:
    """多模块 Maven：把整 reactor 的 mvn 命令改写成只编【改动所在模块】(-pl <mod> -am)。

    RuoYi 等多模块工程根 pom 聚合 6 个模块，整 reactor `mvn compile` 需要所有模块
    源码齐备(而 worker 只同步改动模块) → reactor 失败。正确做法是 -pl 限定改动模块、
    -am 连带构建其依赖的上游模块。已含 -pl 的命令不动；非 mvn 命令原样返回。
    """
    if "mvn" not in command or "-pl" in command:
        return command
    modules = _maven_modules(project_path)
    if not modules:
        return command
    # 从改动文件路径推断所属模块(取路径首段命中 module 名)
    hit: list[str] = []
    for f in modified:
        seg = str(f).strip().split("/")[0]
        if seg in modules and modules[seg] not in hit:
            hit.append(modules[seg])
    if not hit:
        return command
    pl = ",".join(hit)
    # 插到 mvn 之后：mvn <args> → mvn -pl <pl> -am <args>
    return command.replace("mvn", f"mvn -pl {pl} -am", 1)


def run_l1_pipeline(
    project_path: str,
    subtask: SubTask,
    diff: str,
    *,
    timeout: int = 120,
    llm: BaseChatModel | None = None,
) -> tuple[bool, dict[str, Any]]:
    """L1.1 scope → L1.2 compile → L1.2.5 lint → L1.3 scoped test → L1.4 LLM 自检。

    Args:
        project_path: 项目根目录
        subtask: 子任务定义
        diff: 变更 diff
        timeout: 各阶段超时秒数
        llm: 可选 LLM 句柄，用于 L1.4 自检阶段；不传则自检跳过
    """
    details: dict[str, Any] = {"pipeline": "L1.1-L1.4"}

    # ── L1.1 scope 检查 ──
    violations = _scope_violations(diff, subtask.scope)
    details["l1_1_scope_ok"] = not violations
    details["scope_violations"] = violations
    if violations:
        return False, details

    modified = files_from_unified_diff(diff)
    details["modified_files"] = modified

    harness = getattr(subtask, "harness", None)
    _has_verify = bool(getattr(harness, "verify_commands", None)) if harness else False
    if not modified and not _has_verify:
        details["l1_2_compile_ok"] = True
        details["lint"] = {"status": "skipped", "reason": "no files"}
        details["l1_3_test_ok"] = True
        details["note"] = "no diff changes"
        return True, details

    # ── L1.2 编译(语法) ──
    compile_ok, compile_msg = _compile_files(project_path, modified, timeout=timeout)
    details["l1_2_compile_ok"] = compile_ok
    details["compile_message"] = compile_msg
    if not compile_ok:
        return False, details

    # ── L1.2.1 harness.build_command 编译闸门（Java/Go/Rust 等需工具链语言）──
    # _compile_files 仅覆盖 py/js 语法检查；Java(mvn)/Go(go build)/Rust(cargo)
    # 的真实编译靠 Brain 编写的 harness.build_command，在沙箱里跑(那里有工具链)。
    # 这是补齐 5 语言生产级编译验证的关键——杜绝"Java 改坏了但确定性层不知道"。
    build_cmd = getattr(harness, "build_command", "") if harness else ""
    if build_cmd:
        build_cmd = _scope_maven_command(build_cmd, project_path, modified)
    if build_cmd and _build_cmd_applicable(build_cmd, project_path):
        logger.info("[L1.2.1] 执行构建闸门: %s", build_cmd)
        b_ec, b_out = _run_l1_command(build_cmd, project_path, timeout=max(timeout, 300))
        build_ok = b_ec == 0
        details["l1_2_1_build_ok"] = build_ok
        details["build_command"] = build_cmd
        details["build_output"] = compress_tool_output(b_out, max_chars=1500)
        logger.info("[L1.2.1] 构建闸门结果: exit=%s ok=%s", b_ec, build_ok)
        if not build_ok:
            details["build_failed"] = build_cmd
            return False, details
    elif build_cmd:
        details["l1_2_1_build_ok"] = True
        details["build_skipped"] = f"工程文件缺失，跳过构建闸门: {build_cmd}"
        logger.info("[L1.2.1] 跳过构建闸门(无对应工程文件): %s", build_cmd)

    # ── L1.2.0 自动格式化（L0 闸门）──
    # 在 lint 之前先确定性格式化改动文件：把"风格"从模型负担降级为系统自动行为。
    # SWARM_WORKER_L1_FORMAT=false 可关闭。工具缺失优雅 skip，绝不阻断。
    format_enabled = os.environ.get("SWARM_WORKER_L1_FORMAT", "true").lower() not in ("false", "0", "no")
    if format_enabled and modified:
        try:
            from swarm.worker.format_gate import format_files

            fmt_result = format_files(project_path, modified, timeout=timeout)
            details["format"] = fmt_result
        except Exception as exc:  # noqa: BLE001
            # 格式化失败绝不阻断主流程（纯锦上添花层）
            logger.debug("L0 format 跳过(异常): %s", exc)
            details["format"] = {"status": "skipped", "error": str(exc)}

    # ── L1.2.5 lint ──
    lint_enabled = os.environ.get("SWARM_WORKER_L1_LINT", "true").lower() not in ("false", "0", "no")
    if lint_enabled:
        lint_has_error, lint_msg, lint_issues = _lint_files(project_path, modified, timeout=timeout)
        details["lint"] = {
            "status": "error" if lint_has_error else "ok",
            "message": lint_msg,
            "issues": lint_issues,
            "has_error": lint_has_error,
        }
        if lint_has_error:
            # 语法级 lint error（ruff E9xx/F4xx、eslint error）是确定性真错误，
            # 默认硬阻断流水线（确定性断言优于事后告警）。
            # SWARM_WORKER_L1_LINT_GATE=false 可回退到旧的"仅警告不阻断"行为。
            gate_enabled = os.environ.get(
                "SWARM_WORKER_L1_LINT_GATE", "true"
            ).lower() not in ("false", "0", "no")
            error_issues = [i for i in lint_issues if i.get("severity") == "error"]
            details["lint"]["error_issues"] = error_issues
            if gate_enabled:
                details["lint"]["note"] = "lint 语法级 error 硬阻断流水线"
                details["lint"]["gated"] = True
                return False, details
            else:
                details["lint"]["note"] = "lint error 仅作警告（SWARM_WORKER_L1_LINT_GATE=false）"
                details["lint"]["gated"] = False
    else:
        details["lint"] = {"status": "disabled", "reason": "SWARM_WORKER_L1_LINT=false"}

    # ── L1.3 scoped test ──
    # 优先用 Brain 编排的 harness.test_command（精心编写、确定性）；
    # 没有 harness 时才回退到启发式 _guess_test_cmd。（harness 已在上方取得）
    harness_test = getattr(harness, "test_command", "") if harness else ""
    test_cmd = harness_test or _guess_test_cmd(project_path, modified)
    if test_cmd:
        test_cmd = _scope_maven_command(test_cmd, project_path, modified)
    details["test_cmd"] = test_cmd
    details["test_cmd_source"] = "harness" if harness_test else "heuristic"
    if not test_cmd:
        details["l1_3_test_ok"] = True
        details["test_skipped"] = True
    elif not _build_cmd_applicable(test_cmd, project_path):
        # 测试工具的工程文件缺失(npm test 无 package.json 等)→ 跳过，不误判失败
        details["l1_3_test_ok"] = True
        details["test_skipped"] = f"工程文件缺失，跳过测试: {test_cmd}"
        logger.info("[L1.3] 跳过测试(无对应工程文件): %s", test_cmd)
    else:
        t_ec, t_out = _run_l1_command(test_cmd, project_path, timeout=timeout)
        test_ok = t_ec == 0
        details["l1_3_test_ok"] = test_ok
        # 智能压缩：提取关键失败信号行（FAILED/Error/Traceback/assert），
        # 替代盲目硬截断 —— 避免丢失位于输出末尾的 pytest 失败摘要。
        details["test_output"] = compress_tool_output(t_out, max_chars=1500)
        if t_ec == 124:
            details["test_output"] = "test timeout"
        if not test_ok:
            return False, details

    # ── L1.3.5 harness 验收命令（verify_commands）——
    # Brain 为每条验收标准编写的烟雾测试/断言，硬阻断。这是"产出是否合格"的
    # 确定性证据，杜绝 LLM 口头自报合格。
    verify_cmds = list(getattr(harness, "verify_commands", []) or []) if harness else []
    if verify_cmds:
        verify_results = []
        for vc in verify_cmds:
            v_ec, v_out = _run_l1_command(vc, project_path, timeout=timeout)
            ok = v_ec == 0
            verify_results.append({
                "cmd": vc, "ok": ok,
                "output": compress_tool_output(v_out, max_chars=500),
            })
            if not ok:
                details["verify_commands"] = verify_results
                details["verify_failed"] = vc
                return False, details
        details["verify_commands"] = verify_results

    # ── L1.4 LLM 自检（可选，不硬阻断） ──
    self_review_enabled = os.environ.get("SWARM_WORKER_L1_SELF_REVIEW", "true").lower() not in ("false", "0", "no")
    if self_review_enabled and llm is not None:
        review_result = _run_self_review(llm, subtask, diff, timeout=timeout)
        details["self_review"] = review_result
        if not review_result.get("passed", True):
            # 自检发现问题，仅作为警告，不硬阻断
            details["self_review"]["note"] = "LLM 自检发现潜在问题，作为警告（不阻断）"
    elif not self_review_enabled:
        details["self_review"] = {"status": "disabled", "reason": "SWARM_WORKER_L1_SELF_REVIEW=false"}
    else:
        details["self_review"] = {"status": "skipped", "reason": "llm not provided"}

    return True, details
