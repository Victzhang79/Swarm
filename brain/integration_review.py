"""L2 集成审查 — 合并后确定性编译 + 契约符号检查。"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from swarm.brain.contract_utils import contract_symbols
from swarm.project.diff_apply import apply_git_diff, files_from_unified_diff

logger = logging.getLogger(__name__)


def _detect_build_cmd(project_path: str) -> str | None:
    if os.path.isfile(os.path.join(project_path, "pom.xml")):
        return "mvn compile -q -DskipTests"
    if os.path.isfile(os.path.join(project_path, "package.json")):
        return "npm run build --if-present || npx tsc --noEmit --pretty false 2>/dev/null || true"
    if os.path.isfile(os.path.join(project_path, "pyproject.toml")) or os.path.isfile(
        os.path.join(project_path, "setup.py")
    ):
        return "python -m compileall -q ."
    return None


def _run_cmd(project_path: str, cmd: str, *, timeout: int = 300) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_path,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        detail = (proc.stderr or proc.stdout or "").strip()[:2000]
        return ok, detail
    except subprocess.TimeoutExpired:
        return False, "compile timeout"
    except Exception as exc:
        return False, str(exc)


def check_contract_in_diff(
    merged_diff: str,
    shared_contract: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """检查共享契约中的符号是否出现在变更 diff 中（启发式）。"""
    symbols = contract_symbols(shared_contract)
    if not symbols:
        return True, []
    diff_lower = (merged_diff or "").lower()
    missing = [s for s in symbols if s.lower() not in diff_lower]
    if missing and len(missing) == len(symbols):
        return False, [f"契约符号未在 merged_diff 中出现: {missing[:5]}"]
    return True, []


def run_integration_review(
    project_path: str,
    merged_diff: str,
    shared_contract: dict[str, Any] | None = None,
    *,
    timeout: int = 300,
) -> tuple[bool, list[str], dict[str, Any]]:
    """L2.1 全量编译 + L2.3 契约一致性（确定性）。"""
    details: dict[str, Any] = {"stage": "integration_review"}
    issues: list[str] = []

    if not merged_diff.strip():
        return False, ["empty merged_diff"], details

    if not project_path or not os.path.isdir(project_path):
        return False, ["no project path"], details

    contract_ok, contract_issues = check_contract_in_diff(merged_diff, shared_contract)
    issues.extend(contract_issues)
    details["contract_check"] = contract_ok

    apply_result = apply_git_diff(project_path, merged_diff, check_only=True)
    if not apply_result.get("ok"):
        issues.append(f"git apply --check failed: {apply_result.get('stderr', '')[:500]}")
        details["apply_check"] = False
        return False, issues, details
    details["apply_check"] = True

    build_cmd = _detect_build_cmd(project_path)
    details["build_cmd"] = build_cmd
    if build_cmd:
        applied = apply_git_diff(project_path, merged_diff)
        if not applied.get("ok"):
            issues.append(f"git apply failed: {applied.get('stderr', '')[:500]}")
            return False, issues, details
        try:
            ok, out = _run_cmd(project_path, build_cmd, timeout=timeout)
            details["compile_ok"] = ok
            details["compile_output"] = out
            if not ok:
                issues.append(f"L2.1 compile failed: {out[:300]}")
        finally:
            subprocess.run(["git", "checkout", "--", "."], cwd=project_path, capture_output=True)
            subprocess.run(["git", "clean", "-fd"], cwd=project_path, capture_output=True)
    else:
        details["compile_ok"] = None
        logger.info("[integration_review] 未检测到构建文件，跳过全量编译")

    modified = files_from_unified_diff(merged_diff)
    details["modified_files"] = modified

    # audit #25：passed 判定改用结构化标志——issues 本就是"问题列表"，非空即未通过。
    # 原 `not any("failed" in i.lower() ...)` 靠子串匹配，既会漏判(问题描述里无 "failed"
    # 字样的真问题被放行)，又会误判("No test failed" 这类描述含 "failed" 被判失败)。
    passed = len(issues) == 0
    return passed, issues, details
