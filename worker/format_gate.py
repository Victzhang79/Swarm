"""L0 自动格式化闸门（规范执行金字塔最底层）。

理念：风格争论零成本消灭——格式化是确定性的，根本不该进 prompt 让模型纠结，
也不该靠 lint 报错。在 L1 lint 之前先自动格式化改动文件，把"风格"从
"模型要记的规范"降级为"系统自动做的事"。

设计：
- 每语言用其事实标准格式化器（black/ruff、prettier、gofmt、rustfmt、
  google-java-format）。
- 工具缺失一律优雅 skip（shutil.which 探测），绝不阻断主流程。
- 只格式化【改动的文件】，不全仓重排（避免巨 diff 污染）。
- 幂等：格式化器本身幂等，重复运行无副作用。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess

logger = logging.getLogger(__name__)

# 语言 → (探测的可执行名, 构造命令的函数)。命令对【单个文件】原地格式化。
_FORMATTERS: dict[str, list[tuple[str, list[str]]]] = {
    # python: 优先 ruff format（快），退化 black
    "python": [
        ("ruff", ["ruff", "format"]),
        ("black", ["black", "-q"]),
    ],
    "node": [
        ("prettier", ["prettier", "--write", "--log-level", "warn"]),
    ],
    "go": [
        ("gofmt", ["gofmt", "-w"]),
    ],
    "rust": [
        ("rustfmt", ["rustfmt", "--edition", "2021"]),
    ],
    "java": [
        ("google-java-format", ["google-java-format", "-i"]),
    ],
}

_EXT_TO_LANG = {
    ".py": "python",
    ".js": "node", ".jsx": "node", ".ts": "node", ".tsx": "node",
    ".go": "go",
    ".rs": "rust",
    ".java": "java", ".kt": "java",
}


def _which(name: str) -> str | None:
    # 优先 venv 内（ruff 常装在 .venv）
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cand = os.path.join(here, ".venv", "bin", name)
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return shutil.which(name)


def format_files(
    project_path: str, files: list[str], *, timeout: int = 60
) -> dict[str, object]:
    """对改动文件做语言相关自动格式化（L0）。

    Returns: {"formatted": [...], "skipped": [...], "status": "ok"|"partial"}。
    工具缺失/失败一律记录并 skip，绝不抛异常阻断主流程。
    """
    formatted: list[str] = []
    skipped: list[str] = []

    # 按语言分组改动文件
    by_lang: dict[str, list[str]] = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        lang = _EXT_TO_LANG.get(ext)
        if lang:
            by_lang.setdefault(lang, []).append(f)

    for lang, lang_files in by_lang.items():
        # 选第一个可用的格式化器
        chosen: list[str] | None = None
        for exe, cmd in _FORMATTERS.get(lang, []):
            exe_path = _which(exe)
            if exe_path:
                chosen = [exe_path] + cmd[1:]
                break
        if not chosen:
            skipped.extend(lang_files)
            logger.debug("L0 format: %s 无可用格式化器，跳过 %d 文件", lang, len(lang_files))
            continue

        for fp in lang_files[:50]:
            try:
                proc = subprocess.run(
                    chosen + [fp],
                    cwd=project_path,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if proc.returncode == 0:
                    formatted.append(fp)
                else:
                    skipped.append(fp)
                    logger.debug("L0 format 跳过 %s: %s", fp, (proc.stderr or "")[:200])
            except subprocess.TimeoutExpired:
                skipped.append(fp)
                logger.debug("L0 format 超时: %s", fp)
            except Exception as exc:  # noqa: BLE001
                skipped.append(fp)
                logger.debug("L0 format 异常 %s: %s", fp, exc)

    status = "ok" if not skipped else ("partial" if formatted else "skipped")
    return {"formatted": formatted, "skipped": skipped, "status": status}
