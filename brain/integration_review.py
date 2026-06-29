"""L2 集成审查 — 合并后确定性编译 + 契约符号检查。"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from swarm.brain.contract_utils import contract_symbols
from swarm.project.diff_apply import apply_git_diff, files_from_unified_diff

logger = logging.getLogger(__name__)


def _reset_worktree_to_head(project_path: str, merged_diff: str) -> None:
    """把 merged_diff 涉及的文件 reset 到干净的补丁基线（清除 worker pull-back 写入的脏改动）。

    精准处理补丁涉及的文件（不动工作区其他文件）。非 git 仓库或失败时静默跳过。
    - 【已跟踪文件】（HEAD 有）：checkout 回 HEAD 版本，撤销脏改动。
    - 【新建文件】（HEAD 没有，但 worker pull-back 已写进工作区）：删除工作区残留——
      否则 git apply 要新建该文件时报"文件已存在/补丁未应用"（task 691c1670 实证：
      6 文件 CRUD 全是新建，pull-back 写入后 checkout 无效残留 → apply --check 全失败）。
    """
    import os
    try:
        files = files_from_unified_diff(merged_diff) or []
        if not files:
            return
        chk = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        if chk.returncode != 0:
            return
        for f in files:
            # 判断该文件在 HEAD 是否存在（已跟踪 vs 新建）
            in_head = subprocess.run(
                ["git", "-C", project_path, "cat-file", "-e", f"HEAD:{f}"],
                capture_output=True, text=True, timeout=15,
            ).returncode == 0
            if in_head:
                # 已跟踪 → reset 到 HEAD 版本
                subprocess.run(
                    ["git", "-C", project_path, "checkout", "HEAD", "--", f],
                    capture_output=True, text=True, timeout=15,
                )
            else:
                # 新建文件 → 删除工作区残留（pull-back 写入的），让 apply 能干净新建
                abs_f = os.path.join(project_path, f)
                if os.path.isfile(abs_f):
                    try:
                        os.remove(abs_f)
                    except OSError:
                        pass
                # 也从 git index 撤出（worker checkpoint 可能 git add 过）
                subprocess.run(
                    ["git", "-C", project_path, "rm", "--cached", "--force", "-q", f],
                    capture_output=True, text=True, timeout=15,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[L2] reset worktree to HEAD failed (非致命): %s", exc)


def _detect_build_cmd(project_path: str) -> str | None:
    # 检测构建文件 + 对应工具是否【本机实际可用】。L2 全量编译是在本机 project_path 跑，
    # 若本机没装该工具（如未装 maven），不应因环境缺失而误判 L2 失败——L1 闸门已在沙箱里
    # 用真实工具链编译验证过。工具不可用时返回 None → L2 跳过本机编译（task fdaa1932）。
    import shutil
    if os.path.isfile(os.path.join(project_path, "pom.xml")):
        return "mvn compile -q -DskipTests" if shutil.which("mvn") else None
    if os.path.isfile(os.path.join(project_path, "package.json")):
        return (
            "npm run build --if-present || npx tsc --noEmit --pretty false 2>/dev/null || true"
            if shutil.which("npm") else None
        )
    if os.path.isfile(os.path.join(project_path, "pyproject.toml")) or os.path.isfile(
        os.path.join(project_path, "setup.py")
    ):
        return "python -m compileall -q ." if shutil.which("python") else None
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

    # ── 关键(task fdaa1932)：先把工作区 reset 到干净 HEAD 再做 git apply --check ──
    # merged_diff 是【相对 HEAD】生成的补丁。但 worker pull-back 已把改动写进了本地
    # project_path 工作区文件（isXxx/toXxx 方法已存在）→ 工作区是【脏】的。直接在脏工作区
    # git apply --check 会因 "改动已存在、context 已变" 报 "补丁未应用"（假阴性，task
    # fdaa1932 实测）。reset 到 HEAD 后工作区与补丁基线一致，check 才有意义。worker 的脏改动
    # 已被 merged_diff 完整捕获，reset 不丢信息（真正 apply 在下方 build_cmd 分支重新做）。
    _reset_worktree_to_head(project_path, merged_diff)

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
            # R1：限定回滚到 merged_diff 涉及的文件（复用 _reset_worktree_to_head 的 scoped 逻辑：
            # 已跟踪→checkout HEAD，新建→删除），不再用整库 `checkout -- .` + `clean -fd`——
            # 后者会抹掉用户在该项目里无关的未提交改动/未跟踪文件。
            _reset_worktree_to_head(project_path, merged_diff)
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
