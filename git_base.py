"""git_base.py — 任务级 base-commit 钉扎的单一事实源（叶子模块，无 swarm import）。

3rd#2 治本：任务交付链的 git 基线原本散在多处各读【实时 HEAD】，仅当任务运行期 HEAD 不动才自洽。
真实任务跑 7–8h，期间用户或同项目跨模块兄弟任务的 commit 会推进 HEAD → 沙箱/worker/merge/L2/learn
读到不同基线 → 混基线 → apply 失败（好 hunk 被剔）或静默覆盖用户改动。

治本：任务启动（run_task）时 `capture_base_commit` 钉住 `git rev-parse HEAD`，落 task_records.base_commit +
seed 进 BrainState；全链读侧统一 `resolve_base_ref(base)` 拼 git 命令。base=None（非 git/greenfield/GC）
→ 退回 "HEAD" 字面 = 完全现行为，零回归。resume 从 DB 读回不重捕获（base=任务出生基线）。

纯 stdlib，无 swarm import → 可被 worker 与 brain 双侧引用而不引入循环依赖。
"""

from __future__ import annotations

import subprocess


def resolve_base_ref(base_commit: str | None) -> str:
    """把钉扎的 base commit 解析为 git 命令可用的 ref。

    None（非 git 仓 / greenfield / 未捕获 / 被 GC）→ "HEAD"：退回各站点原行为，零回归。
    非空 → 原样返回（40-hex SHA，稳定不漂）。
    """
    base = (base_commit or "").strip()
    return base or "HEAD"


def capture_base_commit(project_path: str | None) -> str | None:
    """任务启动时钉住项目工作区当前 HEAD 的完整 SHA。

    返回 40-hex SHA；非 git 仓库 / git 不可用 / 空路径 → None（调用方据此退回 "HEAD"）。
    只读，不改仓库状态。
    """
    if not project_path:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = (proc.stdout or "").strip()
    # 完整 40-hex（含未来 sha256 仓库的 64-hex）才视为有效 SHA，否则退回 None。
    if sha and len(sha) >= 7 and all(c in "0123456789abcdef" for c in sha.lower()):
        return sha
    return None


def worktree_diverged_from_base(project_path: str | None, base_commit: str | None) -> tuple[bool, str | None]:
    """检测交付时工作区 HEAD 是否已偏离任务钉扎的 base（运行期用户/兄弟任务推进了 HEAD）。

    返回 (diverged, current_head)。base=None 或非 git → (False, None)（无钉扎不谈偏离）。
    偏离本身不阻断交付（reset 到 base + apply 仍产出任务变更），但必须【可观测】——否则 base-pin
    后把文件复位到 base 会把用户中途 commit 的同名文件改动确定性覆盖（3rd-P1b），静默即数据丢失。
    """
    if not project_path or not base_commit:
        return False, None
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    if proc.returncode != 0:
        return False, None
    head = (proc.stdout or "").strip()
    return (bool(head) and head != base_commit), (head or None)


def files_changed_since_base(
    project_path: str | None, base_commit: str | None, files: list[str] | None,
) -> list[str]:
    """交付涉及文件里，哪些在 base..HEAD 之间被【提交过】改动（用户/兄弟任务的中途 commit）。

    这些正是「reset 到 base 会覆盖其已提交改动」的受害文件（3rd-P1b）。空/非 git/无 base → []。
    只读，供交付前 loud 告警 + audit，不改仓库。
    """
    if not project_path or not base_commit or not files:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_path), "diff", "--name-only",
             f"{base_commit}..HEAD", "--", *files],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]


def uncommitted_changed_files(project_path: str | None, files: list[str] | None) -> list[str]:
    """交付涉及文件里，哪些有【未提交】的本地改动（工作区/暂存区脏，HEAD 未动也算）。

    ★B6 复核 #3★：worktree_diverged_from_base 只比 HEAD SHA，漏了"用户改了但没 commit"的场景——
    _reset_worktree_to_head 的 `checkout base -- file` 会静默抹掉这些未提交编辑。用 git status
    --porcelain 探测,供交付前 loud 告警(不再给"偏移已可观测"的错觉)。空/非 git → []。只读。
    """
    if not project_path or not files:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_path), "status", "--porcelain", "--", *files],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    dirty: list[str] = []
    for ln in (proc.stdout or "").splitlines():
        # porcelain 行格式: "XY <path>"（XY 为状态码）。取路径段。
        path = ln[3:].strip() if len(ln) > 3 else ""
        if path:
            dirty.append(path)
    return dirty


def base_ref_exists(project_path: str | None, base_commit: str | None) -> bool:
    """探测钉扎的 base commit 在仓库里仍可达（未被 GC / reset --hard 删除）。

    供交付前分叉检测：base 不可达 → 调用方 loud-fallback 到 HEAD，不静默用坏 ref。
    base 为 None 或非 git → False（调用方退回 HEAD 行为）。
    """
    if not project_path or not base_commit:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(project_path), "cat-file", "-e", f"{base_commit}^{{commit}}"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0
