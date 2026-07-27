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

A-P1-10 决策：格式化【刻意保持本地执行】，不走沙箱优先。
  原因：L1 闸门前已把可写文件从沙箱 pull-back 到本地，格式化必须作用于【本地这份】
  才能体现在产出 diff 里；若改为沙箱里格式化，本地副本不变 → 格式化对产出无效。
  且各语言格式化器是逐文件操作(gofmt/rustfmt/prettier 单文件)，本地可写文件齐备即正确，
  不存在 compile/lint 的"部分树假 PASS/假错"问题。工具缺失(如本机无 gofmt)优雅 skip。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_CARGO_EDITION_RE = re.compile(r'^\s*edition\s*=\s*"(\d{4})"', re.M)


def _rust_edition(project_path: str, rel_fp: str) -> str:
    """D14：rustfmt edition 取最近祖先 Cargo.toml 的 edition 字段。

    有 Cargo.toml 无 edition 字段 → "2015"（cargo/rustfmt 共同缺省）；向上 8 层找不到
    Cargo.toml → "2021"（现代 crate 常态，旧行为）。
    """
    d = os.path.dirname(os.path.join(project_path, rel_fp))
    root = Path(project_path).resolve()
    for _ in range(8):
        cm = os.path.join(d, "Cargo.toml")
        if os.path.isfile(cm):
            try:
                with open(cm, encoding="utf-8", errors="ignore") as fh:
                    m = _CARGO_EDITION_RE.search(fh.read())
            except OSError:
                m = None
            return m.group(1) if m else "2015"
        nd = os.path.dirname(d)
        # 批次6 R1（reviewer LOW）：边界判定用路径语义 is_relative_to——字符串前缀
        # 会把 /tmp/test_a 误判为 /tmp/test 内。
        if nd == d or not Path(d).resolve().is_relative_to(root):
            break
        d = nd
    return "2021"

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
        # D14：edition 不写死——按每个文件最近祖先 Cargo.toml 的 edition 字段注入
        # （写死 2021 会把 2018 crate 按 2021 语法误格式化）。
        ("rustfmt", ["rustfmt"]),
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
    ".java": "java",
    # D14：.kt 不再映射 java——google-java-format 不支持 Kotlin，喂进去恒失败空转；
    # Kotlin 无可用格式化器=诚实不碰（不映射=不进 formatted/skipped 分母）。
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
                _cmd = chosen
                if lang == "rust":  # D14：edition 按最近 Cargo.toml，不写死
                    _cmd = chosen + ["--edition", _rust_edition(project_path, fp)]
                proc = subprocess.run(
                    _cmd + [fp],
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
        # D14：超出 50 上限的尾巴必须如实进 skipped——否则 status 谎报 "ok"
        # （第 51+ 文件既没格式化也没记账，调用方以为全量已 format）。
        if len(lang_files) > 50:
            skipped.extend(lang_files[50:])
            logger.warning(
                "L0 format: %s 超出单批 50 上限，%d 文件未格式化（如实记 skipped）",
                lang, len(lang_files) - 50,
            )

    status = "ok" if not skipped else ("partial" if formatted else "skipped")
    return {"formatted": formatted, "skipped": skipped, "status": status}
