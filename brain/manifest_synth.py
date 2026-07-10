"""manifest_synth.py — D1（round38c 主题D）：聚合清单确定性合成（diff 内，纯文本）。

round38c 实证：root pom 终态半坏——66 文件主模块 ruoyi-alarm 未注册进 <modules>
成死代码。机制（forensics_D_theme_code.md）：MERGE 温和出口把超限写者的清单加性
变更丢弃、注释声称"交 post-pass reconcile 兜底"，但 reconcile 唯一持久点在交付期
learn_success——任务死在中途=承诺落空；且 merged_diff 本体任何路径都不被修补。

治本：merge 终局出口直接在【merged_diff 文本内】合成缺失的 <module> 注册——
root pom <modules> 终态成为"diff 内新建模块 pom 集合"的确定性函数，退出 LLM
竞写面，不依赖任务活到交付期。

为什么不用"临时 apply 全树 → reconcile_workspace_manifests → 重生成 diff"：
工作树上还留着【被 rebase 丢弃写者】的 pull-back 编辑与被弃 untracked 半成品，
全量 regen 会把已被 MERGE 判丢的内容复活折回交付 diff（对抗设计审查预判）。
diff 内合成只信 merged_diff 本身 + base 树 root pom，零污染面。

边界（诚实声明）：v1 只处理【根聚合器 pom.xml + 单段路径新模块】（<mod>/pom.xml
带 <parent>）——round38c 两个失联模块（ruoyi-alarm/alarm-interface）均此形态；
多级聚合器/Gradle settings 待 live 实证需要再扩。合成失败任何路径返回原 diff
（fail-open + loud，绝不阻断交付主链）。
"""
from __future__ import annotations

import difflib
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

_NEW_MODULE_POM_RE = re.compile(r"^([^/\s]+)/pom\.xml$")
_MODULE_ENTRY_RE = re.compile(r"<module>\s*([^<\s][^<]*?)\s*</module>")


def base_root_pom_text(project_path: str | None, base_ref: str | None) -> str | None:
    """取钉扎 base 的根 pom.xml 文本；非 git/无根 pom → None（合成整体跳过）。"""
    if not project_path:
        return None
    from swarm.git_base import resolve_base_ref
    try:
        r = subprocess.run(
            ["git", "-C", project_path, "show", f"{resolve_base_ref(base_ref)}:pom.xml"],
            capture_output=True, text=True, timeout=15)
        return r.stdout if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _apply_section_to_text(base_text: str, section_diff: str) -> str | None:
    """在临时目录用 git apply 把单文件 diff 段落到 base 文本上，返回应用后文本。
    失败 → None（调用方 fail-open）。git apply 在非 repo 目录同样工作。"""
    import os
    import tempfile
    try:
        with tempfile.TemporaryDirectory(prefix="swarm_manifest_synth_") as td:
            with open(os.path.join(td, "pom.xml"), "w", encoding="utf-8") as f:
                f.write(base_text)
            with open(os.path.join(td, "_sec.patch"), "w", encoding="utf-8") as f:
                # git 要求补丁文件以换行结尾否则报 corrupt patch（对齐生产两条
                # apply 路径与 dump_merged_diff_for_diagnosis 的既有治法）——
                # merged_diff 本体("\n\n".join)末段不带尾换行，root pom 段恰是
                # 末段时不补=合成沉默失效
                f.write(section_diff if section_diff.endswith("\n")
                        else section_diff + "\n")
            r = subprocess.run(["git", "apply", "_sec.patch"],
                               cwd=td, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return None
            with open(os.path.join(td, "pom.xml"), encoding="utf-8") as f:
                return f.read()
    except (OSError, subprocess.SubprocessError):
        return None


def fold_module_registrations(merged_diff: str,
                              base_root_pom: str | None) -> tuple[str, list[str]]:
    """把 merged_diff 里新建模块 pom 对应的 <module> 注册确定性合成进根 pom 段。

    返回 (新 merged_diff, 本次补注册的模块目录列表)。无需合成/无法合成 → (原 diff, [])。
    纯文本函数（base_root_pom 由调用方注入），可测。"""
    if not merged_diff.strip() or not base_root_pom:
        return merged_diff, []
    if not base_root_pom.endswith("\n"):
        # difflib 全文件 diff 无 "\ No newline at end of file" 支持——base 无尾换行
        # 时合成段对磁盘真实 base apply 语义会漂（自校验用同一文本变量抓不到）→
        # 保守跳过（极罕见形态，loud）
        logger.warning("[MANIFEST-SYNTH] base 根 pom 无尾换行（异常形态）→ 跳过合成")
        return merged_diff, []
    try:
        from swarm.project.diff_apply import split_diff_by_file
        sections = split_diff_by_file(merged_diff)
        # ① diff 内的新建模块 pom（单段路径 + 新文件 + 声明 <parent>）
        new_mods: list[str] = []
        root_section: str | None = None
        for files, text in sections:
            for f in files:
                if f == "pom.xml":
                    if "+++ b/pom.xml" in text:
                        root_section = text
                    else:
                        # 根 pom 段是删除/重命名等异常形态——再叠加 modify 合成段
                        # 会造成同文件冲突段（apply 必炸，且折叠发生在 apply-check
                        # 之后无再校验）→ 整体保守跳过
                        logger.warning(
                            "[MANIFEST-SYNTH] 根 pom 段非 modify/add 形态（删除/重命名？）"
                            "→ 跳过合成")
                        return merged_diff, []
                m = _NEW_MODULE_POM_RE.match(f)
                if (m and ("--- /dev/null" in text or "new file mode" in text)
                        and "<parent" in text):
                    new_mods.append(m.group(1))
        new_mods = list(dict.fromkeys(new_mods))
        if not new_mods:
            return merged_diff, []
        # ② 合并后的根 pom 文本（diff 有根 pom 段 → 落到 base 上；无 → base 原文）
        applied_root = (_apply_section_to_text(base_root_pom, root_section)
                        if root_section else base_root_pom)
        if applied_root is None:
            logger.error("[MANIFEST-SYNTH] 根 pom diff 段无法应用到 base（异常形态）→ 跳过合成")
            return merged_diff, []
        # 既有注册形态归一（./mod、mod/ 与 mod 等价），防重复插入
        registered = {r.strip().removeprefix("./").rstrip("/")
                      for r in _MODULE_ENTRY_RE.findall(applied_root)}
        missing = [d for d in new_mods if d not in registered]
        if not missing:
            return merged_diff, []
        if "</modules>" not in applied_root:
            logger.warning(
                "[MANIFEST-SYNTH] 根 pom 无 <modules> 块，无法确定性补注册 %s（保守跳过）",
                missing)
            return merged_diff, []
        # ③ 缩进对齐既有 <module> 行（无既有行退默认 8 空格）；整行替换 </modules>
        # 闭合行，保原缩进
        _indent_m = re.search(r"^([ \t]*)<module>", applied_root, re.MULTILINE)
        indent = _indent_m.group(1) if _indent_m else "        "
        insertion = "".join(f"{indent}<module>{d}</module>\n" for d in missing)
        new_root = re.sub(r"^([ \t]*)</modules>",
                          insertion + r"\1</modules>",
                          applied_root, count=1, flags=re.MULTILINE)
        # ④ 根 pom 全文件 diff（相对 base）重生成并回拼。折叠点在 merge 的
        # apply-check 之后（无整体再校验）→ 合成段必须在此自校验：能干净 apply
        # 回 base 且注册行确实在，否则宁可放弃合成也绝不污染交付 diff。
        new_root_section = _full_file_diff("pom.xml", base_root_pom, new_root)
        _self_check = _apply_section_to_text(base_root_pom, new_root_section)
        if _self_check is None or any(
                f"<module>{d}</module>" not in _self_check for d in missing):
            logger.error(
                "[MANIFEST-SYNTH] 合成段自校验失败（apply 不干净或注册未生效）→ 沿用原 diff")
            return merged_diff, []
        # merged_diff 本体（merge_engine "\n\n".join）末段【不带尾换行】——直接
        # "".join 追加会把新段 `diff --git` 头粘进前段末行（补丁损坏且折叠点在
        # apply-check 之后无复检）→ 每段拼接前归一尾换行
        rebuilt: list[str] = []
        replaced = False
        for files, text in sections:
            if root_section is not None and text == root_section:
                rebuilt.append(new_root_section)
                replaced = True
            else:
                rebuilt.append(text if text.endswith("\n") else text + "\n")
        if not replaced:
            rebuilt.append(new_root_section)
        logger.warning(
            "[MANIFEST-SYNTH] D1 聚合清单合成：根 pom 补注册 %d 个新模块 %s"
            "（diff 内确定性合成，退出 LLM 竞写面）", len(missing), missing)
        return "".join(rebuilt), missing
    except Exception as exc:  # noqa: BLE001 — 合成是增强面，绝不阻断交付
        logger.error("[MANIFEST-SYNTH] 合成异常（返回原 diff）: %s", exc)
        return merged_diff, []


def _full_file_diff(rel: str, old: str, new: str) -> str:
    """git 风格全文件 unified diff（modify 语义）。"""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    return f"diff --git a/{rel} b/{rel}\n{body}"
