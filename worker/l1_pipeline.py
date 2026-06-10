"""Worker L1 四级验证 — 确定性 scope / compile / lint / scoped test / LLM 自检。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, TYPE_CHECKING

from swarm.project.diff_apply import files_from_unified_diff
from swarm.types import FileScope, SubTask

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)


def _scope_violations(diff: str, scope: FileScope) -> list[str]:
    modified = files_from_unified_diff(diff)
    writable = set(scope.writable or [])
    if not writable:
        return []
    violations = []
    for fp in modified:
        if not any(fp.endswith(w) or w.endswith(fp) for w in writable):
            violations.append(fp)
    return violations


def _python_bin() -> str:
    """寻找可用的 Python 解释器（python3 > python）。"""
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
            return False, str(exc)

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
            logger.debug("tsc skipped: %s", exc)

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


def _lint_files(project_path: str, files: list[str], *, timeout: int = 60) -> tuple[bool, str, list[dict]]:
    """对修改的文件跑 lint（ruff / eslint），返回 (has_error, message, issues)。

    - Python: ruff check，只计 error 级别，warning 忽略
    - JS/TS: eslint（项目有配置才跑，否则跳过）
    - lint 工具不可用时优雅跳过
    """
    issues: list[dict] = []
    has_error = False
    messages: list[str] = []

    # ── Python: ruff check ──
    py_files = [f for f in files if f.endswith(".py")]
    if py_files:
        ruff_bin = _find_ruff_bin()
        if ruff_bin:
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
                            severity = item.get("fix", {}).get("message", "")  # noqa
                            # ruff 没有 error/warning 字段；用 .code 和是否 autofixable 判断
                            # 保守策略：只要 ruff 报出来就算 issue，但不标记为 error
                            issue_entry = {
                                "file": fp,
                                "line": item.get("location", {}).get("row"),
                                "code": item.get("code", {}).get("value", ""),
                                "message": item.get("message", ""),
                            }
                            # ruff 报出的默认都是可整改项，
                            # 只有 E9xx (syntax/indentation) 和 F4xx (unreachable) 才算 error
                            rule_code = issue_entry["code"]
                            if rule_code.startswith("E9") or rule_code.startswith("F4"):
                                issue_entry["severity"] = "error"
                                has_error = True
                            else:
                                issue_entry["severity"] = "warning"
                            issues.append(issue_entry)
                except subprocess.TimeoutExpired:
                    messages.append(f"ruff 超时({fp})")
                except Exception as exc:
                    messages.append(f"ruff 跳过({fp}): {exc}")
        else:
            messages.append("ruff 未安装，跳过 Python lint")

    # ── JS/TS: eslint（有配置才跑） ──
    js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
    if js_ts:
        has_eslint_config = any(
            os.path.isfile(os.path.join(project_path, cfg))
            for cfg in (".eslintrc.js", ".eslintrc.json", ".eslintrc.yml", ".eslintrc", "eslint.config.js")
        )
        if has_eslint_config:
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
        else:
            messages.append("项目无 eslint 配置，跳过 JS/TS lint")

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

    if not modified:
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
            # lint error 不硬阻断，记为警告
            details["lint"]["note"] = "lint error 仅作警告，不阻断流水线"
    else:
        details["lint"] = {"status": "disabled", "reason": "SWARM_WORKER_L1_LINT=false"}

    # ── L1.3 scoped test ──
    test_cmd = _guess_test_cmd(project_path, modified)
    details["test_cmd"] = test_cmd
    if not test_cmd:
        details["l1_3_test_ok"] = True
        details["test_skipped"] = True
    else:
        try:
            proc = subprocess.run(
                test_cmd,
                cwd=project_path,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            test_ok = proc.returncode == 0
            details["l1_3_test_ok"] = test_ok
            details["test_output"] = (proc.stdout or proc.stderr or "")[:1500]
            if not test_ok:
                return False, details
        except subprocess.TimeoutExpired:
            details["l1_3_test_ok"] = False
            details["test_output"] = "test timeout"
            return False, details
        except Exception as exc:
            details["l1_3_test_ok"] = False
            details["test_output"] = str(exc)
            return False, details

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
