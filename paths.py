"""swarm/paths.py — 路径归属安全判定（单一事实源，round24 A5）。

原本 `resolve 后是否落在 root 内`(防 `../` 与 symlink 逃逸)散在 5 处、3 种拼写：
  - diff_apply._rel_within_root：`(root/rel).resolve().relative_to(root.resolve())` try/except
  - ingest：`target == root or root in target.parents`
  - executor 删除守卫：`resolved != root and root not in resolved.parents`（否定形）
  - sandbox sync ×2：`local_path.relative_to(local_root)` try/except
安全关键、易写歪 → 归一到一个审计过的 fail-closed 原语。各调用点保留自己的越界处置
（返回 bool / 跳过记 error / 过滤），只共享"是否归属"这一核心判定。
"""

from __future__ import annotations

from pathlib import Path


def is_within_root(root: Path | str, candidate: Path | str, *, join: bool = False) -> bool:
    """candidate resolve 后是否落在 root resolve 内（含 root 自身）。

    - join=False：candidate 视为（可能绝对的）完整路径，直接 resolve 后判归属（如 ingest 的
      绝对上传路径、executor 已拼好的路径）。
    - join=True：candidate 视为相对片段，先 root/candidate 再 resolve（如 diff rel、sandbox rel）。

    fail-closed：root/candidate 为空或无法 resolve（OSError/ValueError/RuntimeError，如符号链接
    环、超长路径）一律返回 False。resolve() 会展开 symlink，故能挡 symlink 逃逸。
    """
    try:
        r = Path(root).resolve()
        c = (r / candidate).resolve() if join else Path(candidate).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return c == r or r in c.parents
