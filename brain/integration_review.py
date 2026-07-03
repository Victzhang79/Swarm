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


def _detect_build_cmd_generic(project_path: str) -> str | None:
    """据构建文件确定【全量编译命令】——**不** gate 本机工具可用性（编译在项目沙箱按检测版本
    工具链跑；本机是退回路径）。多栈通用。返回 None 仅当【无任何已知构建文件】(如纯 docs)→合理
    跳过编译，非降级。治本 round21：原 `_detect_build_cmd` 把"本机没装工具"和"没有构建"混为一谈
    都返 None → L2 静默跳过编译→假绿。现分离二者：有构建文件即返命令，工具在哪跑由调用方决定。"""
    j = os.path.join
    if os.path.isfile(j(project_path, "pom.xml")):
        return "mvn -q -DskipTests compile"
    if os.path.isfile(j(project_path, "build.gradle")) or os.path.isfile(
        j(project_path, "build.gradle.kts")
    ):
        return "./gradlew -q compileJava 2>/dev/null || gradle -q compileJava"
    if os.path.isfile(j(project_path, "go.mod")):
        return "go build ./..."
    if os.path.isfile(j(project_path, "Cargo.toml")):
        return "cargo build -q"
    if os.path.isfile(j(project_path, "package.json")):
        return "npm run build --if-present || npx tsc --noEmit --pretty false 2>/dev/null || true"
    if os.path.isfile(j(project_path, "pyproject.toml")) or os.path.isfile(
        j(project_path, "setup.py")
    ):
        return "python -m compileall -q ."
    return None


def _local_tool_available(build_cmd: str) -> bool:
    """build_cmd 的首个可执行(mvn/go/cargo/npm/python/gradle/./gradlew)是否在【本机】可用。"""
    import shutil
    first = (build_cmd or "").strip().split()[0] if build_cmd.strip() else ""
    if first.startswith("./"):
        return True  # ./gradlew 等项目内脚本，交由 shell 判定
    return bool(first) and shutil.which(first) is not None


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
    timeout: int = 600,
    compile_runner=None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """L2.1 全量编译 + L2.3 契约一致性（确定性）。

    compile_runner(build_cmd) -> (ran: bool, ok: bool, output: str)：可选【沙箱编译器】。给定则优先在
    项目沙箱(按检测栈版本烤的工具链)跑全 reactor 编译；沙箱不可用退回本机(仅当本机装了该栈工具)。
    治本 round21：二者都不行 → **fail-loud 拒绝假绿**(不再像旧版本机缺 mvn 就静默跳过编译当通过)。
    timeout 默认 600s(全 reactor 编译 + 首轮依赖解析,Blocker C)。"""
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

    build_cmd = _detect_build_cmd_generic(project_path)
    details["build_cmd"] = build_cmd
    if build_cmd:
        applied = apply_git_diff(project_path, merged_diff)
        if not applied.get("ok"):
            issues.append(f"git apply failed: {applied.get('stderr', '')[:500]}")
            return False, issues, details
        # 通用治本：merged_diff apply 后磁盘上所有成员模块目录都在场(ground truth)。在【集成构建前】
        # 对账聚合清单(Maven/Gradle/Cargo/.NET/Go)，使其枚举真实存在的成员——杜绝并行子任务 pull-back
        # 整文件覆盖把成员注册【冲掉】致集成构建【假失败】(找不到模块)。此处是验证态(下方 finally 会
        # reset 工作区)，持久化由交付 commit 处的同一对账器保证(learn_success)。
        try:
            from swarm.worker.workspace_manifest import reconcile_workspace_manifests
            _wm = reconcile_workspace_manifests(project_path)
            if _wm.get("modified_manifests"):
                details["manifest_reconciled"] = _wm.get("added")
                logger.info("[integration_review] 集成构建前对账聚合清单成员: %s", _wm.get("added"))
        except Exception as _exc:  # noqa: BLE001
            logger.debug("[integration_review] 聚合清单对账跳过(异常,不致命): %s", _exc)
        # D2 版本完整性闸门：reconcile 后仍有【内部模块依赖版本无处可得】者 → reactor 解析必失败。
        # 交付前确定性判死(fail-closed)，别等 900s 编译超时才现形。仅 Maven 内部模块，不碰外部依赖。
        try:
            from swarm.worker.workspace_manifest import missing_intra_project_module_versions
            _missing_ver = missing_intra_project_module_versions(project_path)
            if _missing_ver:
                details["missing_intra_module_versions"] = _missing_ver
                issues.append(
                    "L2 pom 版本完整性: 内部模块依赖缺版本且无 dependencyManagement 兜底(reactor 解析必失败): "
                    + "; ".join(_missing_ver[:8])
                )
                logger.warning("[integration_review] D2 版本闸门报缺: %s", _missing_ver[:8])
        except Exception as _exc:  # noqa: BLE001
            logger.debug("[integration_review] D2 版本闸门跳过(异常,不致命): %s", _exc)
        try:
            # 编译执行：优先【项目沙箱】(按检测栈版本烤的工具链,多栈/多版本自动正确)；沙箱不可用
            # 退回【本机】(仅当本机装了该栈工具)。治本 round21：二者都不行 → fail-loud 拒绝假绿。
            ran = False
            ok = False
            out = ""
            if compile_runner is not None:
                try:
                    ran, ok, out = compile_runner(build_cmd)
                except Exception as _cexc:  # noqa: BLE001
                    logger.warning("[integration_review] 沙箱集成编译异常，尝试本机退回: %s", _cexc)
                    ran = False
                if ran:
                    details["compile_env"] = "sandbox"
            if not ran and _local_tool_available(build_cmd):
                ok, out = _run_cmd(project_path, build_cmd, timeout=timeout)
                ran = True
                details["compile_env"] = "local"
            if ran:
                details["compile_ok"] = ok
                details["compile_output"] = out
                if not ok:
                    issues.append(f"L2.1 集成编译失败: {out[:300]}")
            else:
                # 无沙箱 + 本机无该栈工具链 → 无法验证集成编译。绝不假绿放行(round19 死因之一：
                # 本机缺 mvn→静默跳过编译→L2 假绿→把没编译过的代码当"生产级"交付)。
                details["compile_ok"] = None
                details["compile_unverified"] = True
                issues.append(
                    "L2 集成编译无法执行(沙箱不可用且本机缺该栈工具链)——拒绝假绿放行；"
                    "请确保集成验证沙箱/宿主装有目标栈工具链(见 README 运行环境依赖)"
                )
        finally:
            # R1：限定回滚到 merged_diff 涉及的文件（复用 _reset_worktree_to_head 的 scoped 逻辑：
            # 已跟踪→checkout HEAD，新建→删除），不再用整库 `checkout -- .` + `clean -fd`——
            # 后者会抹掉用户在该项目里无关的未提交改动/未跟踪文件。
            _reset_worktree_to_head(project_path, merged_diff)
    else:
        details["compile_ok"] = None  # 真无构建文件(纯 docs/config) → 合理跳过，非降级
        logger.info("[integration_review] 无构建文件(纯 docs/config)，跳过全量编译")

    modified = files_from_unified_diff(merged_diff)
    details["modified_files"] = modified

    # audit #25：passed 判定改用结构化标志——issues 本就是"问题列表"，非空即未通过。
    # 原 `not any("failed" in i.lower() ...)` 靠子串匹配，既会漏判(问题描述里无 "failed"
    # 字样的真问题被放行)，又会误判("No test failed" 这类描述含 "failed" 被判失败)。
    passed = len(issues) == 0
    return passed, issues, details
