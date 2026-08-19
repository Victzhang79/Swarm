"""将 unified diff 应用到项目 git 工作区。"""

from __future__ import annotations

import codecs
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from swarm.paths import is_within_root

logger = logging.getLogger(__name__)


def unquote_git_path(p: str) -> str:
    """git core.quotePath=true（默认）把含非 ASCII/控制字符的路径输出成 C 风格引号串
    （`"a/\\344\\270\\255.txt"`）——字面串当路径用的三重毁形（★32 号文批2 M4★）：
    中文文件名变八进制转义串、引号包住 `a/`/`b/` 前缀使前缀剥离失效、同目录被拆成
    引号/非引号两种键口径。同族先例=verify.py M-3（ls-tree 侧用 -z 关 quoting；
    diff patch 格式没有 -z，只能消费侧反转义）。反转义失败=如实保留原串
    （fail-honest，绝不臆造一个可能更错的路径）+ WARNING 可观测（批2a-R1 F4：
    降级路径至少一次 WARNING，纯静默违反纪律 3）。
    """
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        try:
            # 八进制/标准转义 → 原始字节 → UTF-8（git 对非 ASCII 字节的输出形态）
            return codecs.escape_decode(p[1:-1].encode("utf-8"))[0].decode("utf-8")
        except Exception:  # noqa: BLE001 — 畸形引号串原样保留
            logger.warning("[DIFF] C-quoted 路径反转义失败，原样保留（fail-honest）: %r", p)
            return p
    return p


def strip_diff_path(raw_path: str) -> str:
    """从 `--- `/`+++ ` 行的路径值提取干净相对路径（调用方已剥 4 字符行前缀）。

    D03 根因治本：旧代码 `plus[6:]` 硬假定前缀恒为 `+++ b/`（6 字符）→ 对 `+++ /dev/null`
    切成 "ev/null"。此处只负责识别 `/dev/null` 哨兵、剥 `a/`|`b/` 前缀、去掉可选的
    `\t 时间戳` 后缀。★32 号文批2 M4★：C-quoted 路径必须【先反转义再剥前缀】——
    引号包住 `a/`/`b/`。★批2a-R1 F2★ 原语从 merge_engine 下放本模块（git diff 格式
    的家），merge 主链路（merge_engine / files_from_unified_diff）共用此漏斗。
    ★批2a-R2 hunter LOW-2 登记★ 三处 best-effort 观测面仍自解析未收编
    （dispatch._diff_to_file_changes / security_scan._parse_diff_new_path /
    planning_core._cur_file）——批4 LOW 收口处理，勿再把本函数称作「全仓唯一漏斗」。
    ★rename from/to 行绝不走本函数★：rename 行的路径值没有 `a/`/`b/` 前缀，真名
    `a/x` 会被误剥——rename 行只用 unquote_git_path。
    """
    p = raw_path.strip()
    if "\t" in p:  # `+++ b/foo\t2024-...` 形式的时间戳后缀
        p = p.split("\t", 1)[0].strip()
    p = unquote_git_path(p)
    if p == "/dev/null":
        return "/dev/null"
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def _rel_within_root(root: Path, rel: str) -> bool:
    """P0-3：相对路径 resolve 后是否落在 root 内（防 diff 里 `../` 逃逸出工作区）。

    供 apply 前 fail-closed 预检（diff_paths_escape_root）用。归一到 swarm.paths.is_within_root（A5）。
    （30 号文批14 F-2：原自研快照/回滚链已整链删除——零生产调用点+分支洞=假安全网。）
    """
    return is_within_root(root, rel, join=True)


def diff_paths_escape_root(project_path: str, diff: str) -> list[str]:
    """返回 diff 中越界（逃出 project_path）的路径列表；空=全部合法。供 apply 前 fail-closed 预检。"""
    root = Path(project_path)
    return [rel for rel in files_from_unified_diff(diff) if not _rel_within_root(root, rel)]


def files_from_unified_diff(diff: str) -> list[str]:
    """从 unified diff 提取变更文件路径（去重，保持顺序）。

    #10：除 `+++ b/`(新增/修改的目标)外，还须采集：
      - `--- a/`(变更的源端)：纯删除时 `+++ /dev/null` 被跳过，只有 `--- a/path` 带路径；
      - `rename from/to`：重命名的旧名只出现在 rename from（+++ b/ 仅含新名）。
    漏采集源端路径有两类下游受害（双锚，删分支前两类都要核对）：
      ① `diff_paths_escape_root` 越界预检漏检纯删除段/rename 旧名的逃逸路径（P0-3）；
      ② `split_diff_by_file`/`apply_git_diff_resilient` 及下游枚举点（knowledge hooks 的
        DELETED 事件、L1 scope 复核、integration_review 复位等）漏记纯删除/重命名旧文件。
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
        # ★32 号文批2a-R1 F2★ 收编唯一漏斗 strip_diff_path：旧判据 `+++ b/` 6 字符
        # 硬前缀对 C-quoted 头行（`+++ "b/\344…"`）整行蒸发——本函数喂 D3 闸
        # （nodes merge）、P0-3 越界预检、L1 scope 复核等 8+ 消费点，quoted 文件
        # 对它们全部隐形（fail-open）。门控保持旧特异性（payload 必须以 a//b/ 前缀
        # 或其 quoted 形态开头）：hunk 体内一条删除行若内容以 `-- ` 开头也长成
        # `--- ` 模样，门控不收窄会把删除行内容误采成路径（scope 复核冤判越界）。
        if line.startswith("+++ "):
            payload = line[4:]
            if payload.startswith(("b/", '"b/')):
                _add(strip_diff_path(payload))
        elif line.startswith("--- "):
            payload = line[4:]
            if payload.startswith(("a/", '"a/')):
                _add(strip_diff_path(payload))
        elif line.startswith("rename from "):
            # rename 行无 a//b/ 前缀，只反转义（真名 `a/x` 走 strip_diff_path 会被误剥）。
            _add(unquote_git_path(line[len("rename from "):].strip()))
        elif line.startswith("rename to "):
            _add(unquote_git_path(line[len("rename to "):].strip()))
    return paths


def apply_git_diff(
    project_path: str,
    diff: str,
    *,
    check_only: bool = False,
) -> dict[str, Any]:
    """在项目目录执行 git apply --check 或 git apply。

    git apply 是原子的（任一 hunk 失败则全不落地），且本函数先 `--check` 再 apply——
    失败时工作区保持干净，无需也无从回滚。（30 号文批14 F-2：原 backup_first 快照/
    回滚机制已整链删除——零生产调用点 + restore 分支洞 = 假安全网。）
    """
    if not diff.strip():
        return {"ok": False, "stage": "input", "stderr": "empty diff"}

    # P0-3 fail-closed：apply 前预检 diff 所有目标路径落在 project_path 内，任一越界即整份拒绝
    # （防 `../` 逃逸写工作区外；git apply 自身有防护，此为纵深 + 让越界诊断可见）。
    _escaped = diff_paths_escape_root(project_path, diff)
    if _escaped:
        return {"ok": False, "stage": "boundary",
                "stderr": f"diff 含越界路径(逃出工作区)，fail-closed 拒绝: {_escaped[:5]}"}

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

        applied = subprocess.run(
            ["git", "apply", "--ignore-whitespace", patch_path],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if applied.returncode != 0:
            return {
                "ok": False,
                "stage": "apply",
                "stdout": applied.stdout,
                "stderr": applied.stderr,
            }
        result: dict[str, Any] = {"ok": True, "stage": "apply", "message": "Diff 已应用到工作区"}
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

    # P0-3 fail-closed：分文件 apply 前先整份预检，任一越界即整份拒绝（不逐段落盘越界文件）。
    _escaped = diff_paths_escape_root(project_path, diff)
    if _escaped:
        return {"ok": False, "stage": "boundary", "applied": [], "failed": [],
                "stderr": f"diff 含越界路径(逃出工作区)，fail-closed 拒绝: {_escaped[:5]}"}

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
