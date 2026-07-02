"""将 unified diff 应用到项目 git 工作区。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


def files_from_unified_diff(diff: str) -> list[str]:
    """从 unified diff 提取变更文件路径（去重，保持顺序）。

    #10：除 `+++ b/`(新增/修改的目标)外，还须采集：
      - `--- a/`(变更的源端)：纯删除时 `+++ /dev/null` 被跳过，只有 `--- a/path` 带路径，
        若不采集则快照漏备份→回滚无法恢复被删文件。
      - `rename from/to`：重命名的旧名只出现在 rename from（+++ b/ 仅含新名），
        漏采集则回滚无法还原旧文件。
    采集源端文件后，snapshot_files 会备份其原始内容(existed=True)，restore 据此恢复。
    """
    seen: set[str] = set()
    paths: list[str] = []

    def _add(path: str) -> None:
        path = path.strip()
        if not path or path == "/dev/null":
            return
        if path not in seen:
            seen.add(path)
            paths.append(path)

    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            _add(line[6:])
        elif line.startswith("--- a/"):
            _add(line[6:])  # 源端：覆盖纯删除 + 重命名旧名
        elif line.startswith("rename from "):
            _add(line[len("rename from "):])
        elif line.startswith("rename to "):
            _add(line[len("rename to "):])
    return paths


def snapshot_files(project_path: str, files: list[str]) -> dict[str, Any]:
    """在应用 diff 前，把受影响文件备份到临时目录，返回可回滚的快照句柄。

    回滚不依赖 git（greenfield/非 git 仓库也可用）。记录每个文件的原始内容
    或"原本不存在"标记，restore_snapshot 据此恢复或删除。
    """
    backup_dir = tempfile.mkdtemp(prefix="swarm_rollback_")
    root = Path(project_path)
    entries: dict[str, dict[str, Any]] = {}
    for rel in files:
        src = root / rel
        if src.is_file():
            dst = Path(backup_dir) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                entries[rel] = {"existed": True, "backup": str(dst)}
            except OSError:
                entries[rel] = {"existed": True, "backup": None}
        else:
            # 文件原本不存在（diff 新建）：回滚时应删除
            entries[rel] = {"existed": False, "backup": None}
    return {
        "project_path": project_path,
        "backup_dir": backup_dir,
        "entries": entries,
        "created_at": time.time(),
    }


def restore_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """回滚：用 snapshot_files 的快照恢复文件到应用 diff 之前的状态。"""
    if not snapshot or not snapshot.get("entries"):
        return {"ok": False, "reason": "empty snapshot"}
    root = Path(snapshot["project_path"])
    restored, deleted, failed = 0, 0, 0
    for rel, info in snapshot["entries"].items():
        target = root / rel
        try:
            if info["existed"] and info.get("backup"):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(info["backup"], target)
                restored += 1
            elif not info["existed"]:
                # 原本不存在 → 删除新建的文件
                if target.is_file():
                    target.unlink()
                deleted += 1
        except OSError:
            failed += 1
    return {"ok": failed == 0, "restored": restored, "deleted": deleted, "failed": failed}


def discard_snapshot(snapshot: dict[str, Any]) -> None:
    """应用确认成功后清理快照临时目录。"""
    bd = (snapshot or {}).get("backup_dir")
    if bd and os.path.isdir(bd):
        shutil.rmtree(bd, ignore_errors=True)


def apply_git_diff(
    project_path: str,
    diff: str,
    *,
    check_only: bool = False,
    backup_first: bool = False,
) -> dict[str, Any]:
    """在项目目录执行 git apply --check 或 git apply。

    backup_first=True 时，在真正 apply 前对受影响文件做快照，返回结果含
    'snapshot' 句柄；调用方可在后续验证失败时用 restore_snapshot 回滚，
    成功后用 discard_snapshot 清理。
    """
    if not diff.strip():
        return {"ok": False, "stage": "input", "stderr": "empty diff"}

    # 关键(task bce82e96)：git apply 要求 patch 文件【以换行结尾】，否则最后一行 hunk 被判
    # "corrupt patch at line N"（末行截断）。worker git diff 经 rstrip("\n") 后末尾无换行，
    # 这里补回一个 \n。用【bytes 模式】写，避免文本模式的 universal-newlines 改写 CRLF 的 \r。
    patch_bytes = diff.encode("utf-8")
    if not patch_bytes.endswith(b"\n"):
        patch_bytes += b"\n"
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".patch", delete=False) as tf:
        tf.write(patch_bytes)
        patch_path = tf.name

    try:
        # --ignore-whitespace：关键(task 93159ec3)——RuoYi 等项目源文件是 CRLF，但 worker
        # 产出/归一化后的 diff 是 LF。不忽略空白(行尾)差异会让 git apply 因 context 行 CRLF↔LF
        # 不匹配而 "补丁未应用/损坏"。--ignore-whitespace 让行尾差异不阻断 apply，只比对真实内容。
        check = subprocess.run(
            ["git", "apply", "--check", "--ignore-whitespace", patch_path],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if check.returncode != 0:
            return {
                "ok": False,
                "stage": "check",
                "stdout": check.stdout,
                "stderr": check.stderr,
            }
        if check_only:
            return {"ok": True, "stage": "check", "message": "git apply --check 通过"}

        snapshot = None
        if backup_first:
            snapshot = snapshot_files(project_path, files_from_unified_diff(diff))

        applied = subprocess.run(
            ["git", "apply", "--ignore-whitespace", patch_path],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            # apply 失败：若已建快照则回滚到干净态
            if snapshot:
                restore_snapshot(snapshot)
                discard_snapshot(snapshot)
            return {
                "ok": False,
                "stage": "apply",
                "stdout": applied.stdout,
                "stderr": applied.stderr,
            }
        result: dict[str, Any] = {"ok": True, "stage": "apply", "message": "Diff 已应用到工作区"}
        if snapshot:
            result["snapshot"] = snapshot
        return result
    finally:
        try:
            os.unlink(patch_path)
        except OSError:
            pass


def split_diff_by_file(diff: str) -> list[tuple[list[str], str]]:
    """把 unified diff 按【文件段】拆成可独立 apply 的子 diff。

    git 标准 diff 每文件段以 `diff --git a/… b/…` 起头，自包含(含 index/---/+++/@@ hunks)；
    裸 unified diff(无 `diff --git`)退化为按 `--- `/`+++ ` 文件对边界拆。返回 [(files, sub_diff)]，
    仅保留能提取到目标文件的段(空/前言段丢弃)。用于 apply_git_diff_resilient 的分文件落盘。
    """
    lines = diff.splitlines(keepends=True)
    has_git_hdr = any(ln.startswith("diff --git ") for ln in lines)
    sections: list[list[str]] = []
    cur: list[str] = []
    for i, ln in enumerate(lines):
        if has_git_hdr:
            boundary = ln.startswith("diff --git ")
        else:
            # 无 git 头：文件头对 `--- x` 紧跟 `+++ y` 才是新段开始。要求【下一行是 +++ 】，
            # 避免把 hunk 内被删除的内容行(如 SQL `-- comment` 渲成 `--- comment`)误判成文件边界。
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            boundary = (
                ln.startswith("--- ") and nxt.startswith("+++ ")
                and any(x.startswith("+++ ") for x in cur)
            )
        if boundary and cur:
            sections.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        sections.append(cur)

    out: list[tuple[list[str], str]] = []
    for sec in sections:
        text = "".join(sec)
        files = files_from_unified_diff(text)
        if text.strip() and files:
            out.append((files, text))
    return out


def apply_git_diff_resilient(project_path: str, diff: str) -> dict[str, Any]:
    """分文件鲁棒 apply：整块失败不连坐回滚好文件。

    治本 round18 P0-C：一个坏 hunk 令整块 `git apply` 原子失败 → ~30 个正确 producer 一个没落盘。
    先试整块 apply(全过则最优——顺序/rename 语义完整、单次调用)；失败则按【文件段】独立 apply，
    好段照常落盘、坏段单独剔除记录。返回 {ok, stage, applied:[files], failed:[{files,stage,stderr}]}。
    ok = 至少一个文件落盘。调用方据 applied 决定纳入 commit 的文件集、据 failed 交 owner 重修。
    """
    if not diff.strip():
        return {"ok": False, "stage": "input", "stderr": "empty diff", "applied": [], "failed": []}

    # 快路径：整块原子 apply 成功即最优
    whole = apply_git_diff(project_path, diff, check_only=False)
    if whole.get("ok"):
        return {
            "ok": True, "stage": "apply",
            "applied": files_from_unified_diff(diff), "failed": [],
            "message": whole.get("message", "整块 apply 成功"),
        }

    # 慢路径：按文件段独立 apply，好段保留、坏段剔除（杜绝连坐）
    applied: list[str] = []
    failed: list[dict[str, Any]] = []
    for files, sub in split_diff_by_file(diff):
        res = apply_git_diff(project_path, sub, check_only=False)
        if res.get("ok"):
            applied.extend(files)
        else:
            failed.append({
                "files": files,
                "stage": res.get("stage"),
                "stderr": (res.get("stderr") or "")[:300],
            })
    return {
        "ok": bool(applied),
        "stage": "per_file",
        "applied": applied,
        "failed": failed,
        "message": f"分文件落盘：成功 {len(applied)} 文件，剔除坏段 {len(failed)}",
    }


def commit_task_output(
    project_path: str,
    files: list[str],
    *,
    task_id: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """任务 accept 后把产出 git commit 到本地（仅本地，绝不 push）。

    第二批根因修复（用户选项A）：DONE 后产出 apply 到工作区但【不 commit】，
    后续操作（git checkout / VERIFY_L2 reset / 下个任务）会把未提交的产出冲掉 →
    事实库（磁盘/git/索引）滞后或丢失 → 下个任务事实核验误判"文件不存在"。
    commit 后产出稳定落盘，且天然触发已有的 git 增量索引链路，事实库自洽。

    仅本地 commit，【不 push】（push 由用户拍板）。非 git 仓库 / 无变更 → 跳过。
    返回 {"ok", "committed", "commit_hash"|"reason"}。
    """
    if not files:
        return {"ok": True, "committed": False, "reason": "无变更文件"}
    try:
        chk = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        if chk.returncode != 0:
            return {"ok": True, "committed": False, "reason": "非 git 仓库"}
        # 治本(D5b 配套)：只 add【磁盘真实存在】的文件——resilient apply 剔掉的坏段文件仍缺，
        # 若混进 `git add` 会 pathspec 不匹配令【整批 add 失败 → 一个都不 commit】(好文件白落盘、
        # 被后续 reset 冲掉，恰好没落地 D5b 想救的场景)。过滤后落盘啥就 commit 啥。
        existing = [f for f in files if os.path.exists(os.path.join(project_path, f))]
        if not existing:
            return {"ok": True, "committed": False, "reason": "无落盘文件可提交"}
        # 只 add 本任务产出的文件（精准，不裹挟工作区其他改动）
        add = subprocess.run(
            ["git", "-C", project_path, "add", "--", *existing],
            capture_output=True, text=True, timeout=30,
        )
        if add.returncode != 0:
            return {"ok": False, "committed": False, "reason": f"git add 失败: {add.stderr[:200]}"}
        # 检查是否真有已暂存改动（apply 后内容可能与 HEAD 相同 → 无需 commit）
        staged = subprocess.run(
            ["git", "-C", project_path, "diff", "--cached", "--quiet"],
            capture_output=True, text=True, timeout=15,
        )
        if staged.returncode == 0:
            return {"ok": True, "committed": False, "reason": "无已暂存改动"}
        msg = message or f"swarm task output{f' [{task_id}]' if task_id else ''}"
        # 关闭 GPG 签名 + 设置 author，避免环境缺 user.name/email 时 commit 失败
        commit = subprocess.run(
            ["git", "-C", project_path,
             "-c", "user.name=swarm-agent", "-c", "user.email=swarm@local",
             "-c", "commit.gpgsign=false",
             "commit", "--no-verify", "-m", msg],
            capture_output=True, text=True, timeout=30,
        )
        if commit.returncode != 0:
            return {"ok": False, "committed": False, "reason": f"git commit 失败: {commit.stderr[:200]}"}
        sha = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()[:12]
        return {"ok": True, "committed": True, "commit_hash": sha}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "committed": False, "reason": str(exc)}
