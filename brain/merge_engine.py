"""Merge engine — parse unified diffs, 3-way auto-resolve, conflict detection."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)

BaseReader = Callable[[str], str | None]


@dataclass
class MergeConflict:
    """A file-level merge conflict between subtasks."""

    file_path: str
    subtask_ids: list[str]
    message: str
    auto_resolved: bool = False


@dataclass
class MergeResult:
    merged_diff: str
    conflicts: list[MergeConflict] = field(default_factory=list)
    success: bool = True
    auto_resolved_files: list[str] = field(default_factory=list)
    rebase_subtask_ids: list[str] = field(default_factory=list)
    # D11：硬冲突的标记渲染（诊断专用）——merged_diff 保持可 apply，毒标记绝不下行
    conflict_render: str = ""
    # 6.9-HF3：rebase 来源标记（sid → "new_file"|"three_way"）。over_limit 终点按来源分流：
    # new_file=选中版已在 merged_diff（丢的是落选写者版本）→ D7 口径 abandoned+PARTIAL 继续
    # 交付；three_way=真源码 hunk 被丢 → escalate。缺省视作 three_way（fail-closed）。
    rebase_origin: dict = field(default_factory=dict)


@dataclass
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]
    subtask_id: str

    @property
    def old_end(self) -> int:
        count = self.old_count if self.old_count > 0 else 1
        return self.old_start + count - 1

    def overlaps(self, other: _Hunk) -> bool:
        return not (self.old_end < other.old_start or other.old_end < self.old_start)


@dataclass
class _FilePatch:
    file_path: str
    header_lines: list[str]
    hunks: list[_Hunk]
    # D03：`+++ /dev/null`（或 `deleted file mode`）→ 该段是【删除】意图，输出须表达为
    # `+++ /dev/null` + 全 `-` 行，而非被伪路径归并成新文件后丢掉 `-` 行蒸发。
    is_deletion: bool = False
    # D06：rename / copy / 二进制段无法字符级合并（无 _Hunk 表示），必须【整段透传】保留原文，
    # 绝不静默丢弃。passthrough 非空时 merge_diffs 原样 emit 该段、跳过 hunk 合并。
    passthrough: str | None = None


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip_diff_path(raw_path: str) -> str:
    """从 `--- `/`+++ `/rename 行的路径值提取干净相对路径。

    D03 根因治本：旧代码 `plus[6:]` 硬假定前缀恒为 `+++ b/`（6 字符）→ 对 `+++ /dev/null`
    切成 "ev/null"。正确做法：调用方已剥 `--- `/`+++ ` 前缀（4 字符），此处只负责识别
    `/dev/null` 哨兵、剥 `a/`|`b/` 前缀、去掉可选的 `\t 时间戳` 后缀。
    """
    p = raw_path.strip()
    if "\t" in p:  # `+++ b/foo\t2024-...` 形式的时间戳后缀
        p = p.split("\t", 1)[0].strip()
    if p == "/dev/null":
        return "/dev/null"
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def _parse_git_header_paths(line: str) -> tuple[str, str]:
    """从 `diff --git a/PATH b/PATH` 抽 (old, new) 相对路径（best-effort，含空格路径尽力而为）。"""
    rest = line[len("diff --git "):]
    if rest.startswith("a/") and " b/" in rest:
        a_part, b_part = rest.split(" b/", 1)
        return a_part[2:], b_part
    parts = rest.split()
    if len(parts) == 2:
        return _strip_diff_path(parts[0]), _strip_diff_path(parts[1])
    return "", ""


def _recount_hunk_header(header_line: str, body_lines: list[str]) -> str:
    """据 hunk 实际 body 行重算 @@ 头的 old_count/new_count（保 old_start/new_start 与尾部
    section heading），确保头与体一致 → 杜绝合并/规范化后行数漂移导致的 git apply '补丁损坏'。"""
    m = _HUNK_RE.match(header_line)
    if not m:
        return header_line
    old_c = new_c = 0
    for ln in body_lines:
        c = ln[:1]
        if c == "+":
            new_c += 1
        elif c == "-":
            old_c += 1
        elif c == "\\":
            # `\ No newline at end of file` 是 git 对上一行的零宽注解，old/new 两侧【都不计】。
            # 旧代码误按 context +1/+1：LLM 生成的 .java/.html 普遍无结尾换行 → 每个新文件 diff
            # 都带此标记 → 头从 `@@ -0,0 +1,N @@` 被写成 `@@ -0,1 +1,N+1 @@`，`-0,1` 引用不存在
            # 的旧行 0 → git apply 报"补丁损坏"（996db614 round16 实测 88 个新文件全中）。
            continue
        else:  # 真正的 context 行（" " 开头）：old/new 两侧各 +1
            old_c += 1
            new_c += 1
    tail = header_line[m.end():]  # @@ 后可选的 section heading（如 ' func foo()'）
    return f"@@ -{_parse_int(m.group(1))},{old_c} +{_parse_int(m.group(3))},{new_c} @@{tail}"


def _parse_int(value: str | None, default: int = 1) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _split_raw_diffs(diff: str) -> list[str]:
    """Split a multi-file unified diff into per-file chunks."""
    # 只剥首尾的 \n（不能用 .strip()：会吃掉行内/行尾的 \r 和有意义的尾部空格，
    # 导致 CRLF 项目的 diff 最后一行 context 丢 \r → git apply 损坏。task 4b244174 实测）。
    text = diff.strip("\n")
    if not text:
        return []

    # 用 split("\n") 而非 splitlines()：splitlines 会把行内尾部的 \r 也当行尾去掉，
    # 破坏 CRLF 行尾。split("\n") 只按 \n 拆，每行保留尾部 \r。
    raw_lines = text.split("\n")
    # 重组为带 \n 的行（最后一行不补 \n），保留每行原有的 \r
    lines = [ln + "\n" for ln in raw_lines[:-1]] + [raw_lines[-1]]
    chunks: list[list[str]] = []
    current: list[str] = []

    for idx, line in enumerate(lines):
        # D05 治本：`--- ` 只有在【下一行是 `+++ `】时才是真文件边界。否则 hunk 体内以 `--- `
        # 开头的删除行（如删 SQL/Lua 注释 `-- comment` → diff 行 `--- comment`）会被误当边界、
        # 把文件段从 hunk 中间切开。与 project/diff_apply.split_diff_by_file 的守卫对齐。
        nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
        is_new_file = line.startswith("diff --git ") or (
            line.startswith("--- ") and nxt.startswith("+++ ")
            and current and any(l.startswith("+++ ") for l in current)
        )
        if is_new_file and current:
            chunks.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        chunks.append(current)

    # 每块只剥首尾 \n（保 \r 和尾部空格），空块（剥 \n 后为空）剔除
    out = []
    for c in chunks:
        joined = "".join(c).strip("\n")
        if joined:
            out.append(joined)
    return out


def _parse_file_patch(raw: str, subtask_id: str) -> _FilePatch | None:
    # 用 split("\n") 而非 splitlines()（task f20ea68d 根因·CRLF）：
    # splitlines() 会把行内容尾部的 \r 也当行尾去掉 → CRLF 项目的 diff 经 MERGE 重组后
    # 丢失 \r → git apply 回 CRLF 的 HEAD 时 context 字节不匹配。split("\n") 只按 \n 拆，
    # 保留每行尾部的 \r，使 CRLF diff 经 MERGE 后仍与源文件同行尾。
    # 注意：split("\n") 对以 \n 结尾的文本会在末尾产生一个 "" 元素，下游 _format 会处理。
    lines = raw.split("\n")
    if not lines or (len(lines) == 1 and lines[0] == ""):
        return None

    # ── 先扫非-hunk 元数据行判段类型（D06：rename/copy/binary，D03：deletion 提示）──
    # rename/copy 段可能【无 hunk】、二进制段【无法字符级合并】——都必须整段透传保留而非丢弃。
    # 元数据行(diff --git/rename/copy/GIT binary/Binary files/deleted file mode)恒【无前缀字符】，
    # 而 diff 内容行(context/add/del)恒带 ` `/`+`/`-` 前缀，故 startswith 无前缀匹配不会误伤内容行。
    is_rename = is_binary = is_deletion = False
    rename_old = rename_new = ""
    git_a = git_b = ""
    for ln in lines:
        if ln.startswith("diff --git "):
            ap, bp = _parse_git_header_paths(ln)
            if ap:
                git_a = ap
            if bp:
                git_b = bp
        elif ln.startswith("rename from "):
            is_rename = True
            rename_old = _strip_diff_path(ln[len("rename from "):])
        elif ln.startswith("rename to "):
            is_rename = True
            rename_new = _strip_diff_path(ln[len("rename to "):])
        elif ln.startswith("copy from "):
            is_rename = True
            rename_old = _strip_diff_path(ln[len("copy from "):])
        elif ln.startswith("copy to "):
            is_rename = True
            rename_new = _strip_diff_path(ln[len("copy to "):])
        elif ln.startswith("GIT binary patch") or ln.startswith("Binary files "):
            is_binary = True
        elif ln.startswith("deleted file mode"):
            is_deletion = True

    # D06：rename/copy/binary 无法安全字符级合并 → 整段透传（fail-closed：最差是 apply 冲突【可见】，
    # 远优于静默丢内容）。file_path 仍解析出来供上层做冲突去重键与诊断。
    if is_rename or is_binary:
        fp = rename_new or git_b or rename_old or git_a or "unknown"
        return _FilePatch(file_path=fp, header_lines=[], hunks=[], passthrough=raw)

    file_path = ""
    header_lines: list[str] = []
    hunks: list[_Hunk] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        # 文件头对：`--- OLD` 紧跟 `+++ NEW`。要求相邻的 `+++ `，才不会把 hunk 体内被删除的
        # `--- comment` 行（D05）误当文件头。
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            header_lines.append(line)
            old_path = _strip_diff_path(line[4:])
            plus = lines[i + 1]
            header_lines.append(plus)
            new_path = _strip_diff_path(plus[4:])  # D03：剥 4 字符前缀，正确识别 /dev/null
            i += 2
            if new_path == "/dev/null":
                # 删除：目标是 /dev/null，真实路径在 `--- OLD` 侧。
                is_deletion = True
                if old_path and old_path != "/dev/null":
                    file_path = old_path
            elif new_path:
                file_path = new_path
            elif old_path and old_path != "/dev/null":
                file_path = old_path
            continue

        match = _HUNK_RE.match(line)
        if match:
            hunk_lines = [line]
            i += 1
            # D05：hunk body 收集在遇到下一个 `@@ ` / `diff --git ` / 【真】文件边界(`--- ` 紧跟
            # `+++ `) 时才终止。裸 `--- ` 删除行不再截断 hunk。
            while i < len(lines):
                nxt = lines[i]
                if nxt.startswith("@@ ") or nxt.startswith("diff --git "):
                    break
                if nxt.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
                    break
                hunk_lines.append(nxt)
                i += 1
            hunks.append(
                _Hunk(
                    old_start=_parse_int(match.group(1)),
                    old_count=_parse_int(match.group(2), default=1),
                    new_start=_parse_int(match.group(3)),
                    new_count=_parse_int(match.group(4), default=1),
                    lines=hunk_lines,
                    subtask_id=subtask_id,
                )
            )
            continue

        if line.startswith("diff --git "):
            i += 1
            continue

        i += 1

    if not file_path and not hunks:
        return None
    if not file_path and hunks:
        file_path = git_b or git_a or "unknown"

    return _FilePatch(
        file_path=file_path, header_lines=header_lines, hunks=hunks, is_deletion=is_deletion
    )


def _new_side_lines(hunks: list[_Hunk]) -> list[str]:
    """取所有 hunk 的【新侧】内容(context+addition，丢弃 deletion / @@ 头 / split 空产物 / marker)，
    每行前缀 `+`。用于新文件重建（纯新增 hunk）与新文件多写者去重比对。"""
    out: list[str] = []
    dropped_del = 0
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        for raw in hunk.lines[1:]:              # 跳过 [0] 的 @@ 头
            if raw == "" or raw.startswith("\\"):  # split 尾部产物 "" / `\ No newline` 标记
                continue
            tag, content = raw[0], raw[1:]
            if tag == "-":                       # deletion 在新文件里无意义，丢弃
                dropped_del += 1
                continue
            out.append("+" + content)            # addition 原样；context 转新增（新文件里也是内容）
    if dropped_del:
        # 降级可观测（round29 双复核）：头声明纯新建却带 deletion 行=自相矛盾的 hunk（worker 幻觉/
        # 上游解析边角），内容被丢必须留痕，不得静默蒸发。
        logger.warning(
            "[MERGE] 新文件重建丢弃 %d 行 deletion（-0,0 纯新建 hunk 不应含 `-` 行，子任务 %s）",
            dropped_del, sorted({h.subtask_id for h in hunks}),
        )
    return out


def _format_file_patch(
    file_path: str, header_lines: list[str], hunks: list[_Hunk], is_new: bool = False,
    base_known: bool = False,
) -> str:
    # round29 B：creation 形状（全部 hunk `@@ -0,0`=纯新建）却被判 modify → 提升为纯新建补丁。
    # ★只在 base_known=False（调用方无 base_reader、保守判 modify）时触发★：旧裸传统格式靠 git
    # 对 `-0,0` 的创建启发式侥幸通过，补 `diff --git` 头后需 `new file mode` 语义才对。若调用方有
    # 权威 base 判定（base_known=True）说文件【已存在】（如 base 里的空文件被填充，`-0,0` 是
    # "旧侧空"的合法 modify 形状），绝不能提升——`new file mode` 打已存在文件必报 already exists
    # （双复核 Finding 1 实证回归；git 对带 `diff --git` 头的 `-0,0` modify 本就能干净 apply）。
    if (not is_new and not base_known and hunks
            and all(h.old_start == 0 and h.old_count == 0 for h in hunks)):
        logger.warning(
            "[MERGE] %s 无 base 权威判定且全 hunk 为 -0,0 创建形状 → 按新建补丁输出（子任务 %s）",
            file_path, sorted({h.subtask_id for h in hunks}),
        )
        is_new = True
    if is_new:
        # 新文件（merge base 无此文件，由调用方据 base_reader 权威判定）：必须输出【纯新建补丁】，
        # git apply 据 `--- /dev/null` + `@@ -0,0 +1,N @@` 识别为创建。round17 根因②：新模块 pom 的头
        # 被无条件重写成 `--- a/…` → No such file。且 worker 可能因沙箱 bootstrap 材化了该文件而发
        # modify 风格 hunk(`@@ -1,N` + context) → 即便改了 --- /dev/null，git apply 仍报"新文件依赖旧
        # 内容"。故这里【不保留原 hunk 头/行号】，而是把所有 hunk 的【新侧】(context+addition，丢弃
        # deletion)按序取出作为文件内容，重建成单个 `@@ -0,0 +1,N @@` 纯新增块——对 worker 发 /dev/null
        # 或 --- a/、`@@ -0,0` 或 `@@ -1,N` 全部成立（复现单测坐实）。
        new_lines = _new_side_lines(hunks)
        if not new_lines:
            return ""
        header = [
            f"diff --git a/{file_path} b/{file_path}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{file_path}",
            f"@@ -0,0 +1,{len(new_lines)} @@",
        ]
        return "\n".join(header + new_lines)
    if header_lines:
        header = list(header_lines)
        if header[0].startswith("--- "):
            header[0] = f"--- a/{file_path}"
        if len(header) > 1 and header[1].startswith("+++ "):
            header[1] = f"+++ b/{file_path}"
    else:
        header = [f"--- a/{file_path}", f"+++ b/{file_path}"]
    # round29 B 治本：modify 段也必须带 `diff --git` 头（不带 new file mode，那是新建专属）。
    # merged_diff 混排【git 格式 new-file 段 + 裸传统 modify 段】时，git 进入 git 格式解析模式后
    # 无法为裸块建立文件上下文 → desync 消费到 EOF →「corrupt patch」（task d37a52a3：77 头 79 段，
    # 报第 9208 行=EOF 损坏）。不变量：每个文件段自带 diff --git 头，头数==段数。
    if not header or not header[0].startswith("diff --git "):
        header.insert(0, f"diff --git a/{file_path} b/{file_path}")

    # ── 逐 hunk 规范化 body + 【重算 @@ 头行数】（task 3adfeca5/f20ea68d + 996db614 merge 损坏）──
    # 关键治本：规范化（丢尾部 split 产物 "" / 中间空行 "" 还原为 " "）会改变 hunk body 行数，
    # 但原代码【不更新 @@ 头声明的 old_count/new_count】→ 头与体行数不符 → git apply 报"补丁
    # 损坏"(malformed，996db614 第2822行实测)。逐 hunk 据【实际 body】重算头行数，保证头永远
    # 与体一致 = 补丁永远 well-formed（即便内容有偏差，git apply 也只会干净 reject，不会"损坏"）。
    body: list[str] = []
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        hlines = list(hunk.lines)
        if not hlines:
            continue
        hdr, hbody = hlines[0], hlines[1:]
        while hbody and hbody[-1] == "":          # 丢尾部 split 产物 ""
            hbody.pop()
        hbody = [(" " if ln == "" else ln) for ln in hbody]  # 中间空行 → " " context
        if _HUNK_RE.match(hdr):
            body.append(_recount_hunk_header(hdr, hbody))
            body.extend(hbody)
        else:  # 非标准 hunk（无 @@ 头）：保守原样
            body.append(hdr)
            body.extend(hbody)
    return "\n".join(header + body)


def _format_deletion_patch(file_path: str, hunks: list[_Hunk]) -> str:
    """D03：输出【删除补丁】——`deleted file mode` + `--- a/path` + `+++ /dev/null` + 全 `-` 行。

    删除的 hunk 形如 `@@ -1,N +0,0 @@`（全 `-` 行）。逐 hunk 规范化 body 并重算 @@ 头（复用
    _recount_hunk_header，保头体一致），git apply 据 `+++ /dev/null` 识别为删除。空文件删除
    （无 hunk）→ 仅输出头四行，git 亦能删除 0 字节文件。
    """
    body: list[str] = []
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        hlines = list(hunk.lines)
        if not hlines:
            continue
        hdr, hbody = hlines[0], hlines[1:]
        while hbody and hbody[-1] == "":          # 丢尾部 split 产物 ""
            hbody.pop()
        hbody = [(" " if ln == "" else ln) for ln in hbody]  # 中间空行 → " " context
        if _HUNK_RE.match(hdr):
            body.append(_recount_hunk_header(hdr, hbody))
            body.extend(hbody)
        else:
            body.append(hdr)
            body.extend(hbody)
    header = [
        f"diff --git a/{file_path} b/{file_path}",
        "deleted file mode 100644",
        f"--- a/{file_path}",
        "+++ /dev/null",
    ]
    return "\n".join(header + body)


def _format_conflict_hunks(file_path: str, hunks: list[_Hunk], is_new: bool = False) -> str:
    subtask_ids = list(dict.fromkeys(h.subtask_id for h in hunks))
    # 入口对称（防同类 sibling bug）：新文件冲突也用 /dev/null 头。冲突块含 <<<<< 标记本就不可
    # apply（交人工/rebase），此处仅保持头一致，不改变冲突语义。
    _minus = "--- /dev/null" if is_new else f"--- a/{file_path}"
    # round29 B（复核 MEDIUM）：冲突段同样带 `diff --git` 头，让「头数==段数」不变量无条件成立——
    # 否则混排时 split_diff_by_file 的 git-头边界启发式会把无头冲突段粘连进前一文件段。
    # 头不改变冲突语义（<<<<<<< 标记仍使其不可 apply，照旧交人工/rebase）。
    parts = [
        f"diff --git a/{file_path} b/{file_path}",  # 段首（边界启发式按此切段，注释行不得在其前）
        f"# ═══ MERGE CONFLICT: {file_path} (subtasks: {', '.join(subtask_ids)}) ═══",
        _minus,
        f"+++ b/{file_path}",
    ]
    for hunk in hunks:
        parts.append(f"<<<<<<< {hunk.subtask_id}")
        parts.extend(ln for ln in hunk.lines if ln != "")
        parts.append("=======")
    parts.append(f">>>>>>> {subtask_ids[-1]}")
    return "\n".join(parts)


def _split_lines(text: str) -> list[str]:
    if not text:
        return []
    if text.endswith("\n"):
        return text.splitlines(keepends=True)
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text]
    return lines


def _join_lines(lines: list[str]) -> str:
    return "".join(lines)


class HunkContextMismatch(Exception):
    """D9（阶段6，登记册 §五）：hunk 的 context/删除行与 base 实文本不符（基线漂移）。

    旧行为静默消费 tag 不比对——错位时产出语义损坏的"干净"合并（3-way 输入被污染，
    well-formed 骗过 apply/L2）。fail-closed：调用方捕获后放弃 3-way，落冲突/rebase。"""


def apply_hunk(lines: list[str], hunk: _Hunk) -> list[str]:
    """Apply one unified hunk to line list (1-indexed old_start)。

    D9：context(" ")/删除("-")行与 base 逐行比对（忽略行尾换行差异），不符即抛
    HunkContextMismatch——绝不静默产出错位文本。"""
    idx = max(hunk.old_start - 1, 0)
    result = lines[:idx]
    src_idx = idx
    for raw in hunk.lines[1:]:
        if raw.startswith("\\ No newline"):
            continue
        if not raw:
            # 6.9-RF4①：空串=空白 context 行（LLM 常见产出形态，与 _format_file_patch 的
            # ""→" " 归一对齐）。旧行为跳过且不推进 src_idx → 游标错位，后续第一条
            # context/删除行必误抛 HunkContextMismatch（干净 3-way 被假阳性打落 rebase）。
            raw = " "
        tag = raw[0]
        content = raw[1:] if len(raw) > 1 else ""
        if tag in (" ", "-"):
            _base_line = lines[src_idx] if src_idx < len(lines) else None
            # 6.9-RF4②：比对剥 \r\n（CRLF base × LF diff 混合行尾是真实形态非漂移）；
            # 异常消息与比对同口径（旧消息 rstrip() 全剥，误判时打出两个"相同"串无法排障）。
            _exp = content.rstrip("\r\n")
            _got = _base_line.rstrip("\r\n") if _base_line is not None else None
            if _got is None or _got != _exp:
                raise HunkContextMismatch(
                    f"hunk@{hunk.old_start} {'context' if tag == ' ' else 'delete'} 行与 base 不符: "
                    f"expect={_exp!r} got={('<EOF>' if _got is None else _got)!r}")
        if tag == " ":
            result.append(content if content.endswith("\n") else content + "\n" if content else "\n")
            src_idx += 1
        elif tag == "-":
            src_idx += 1
        elif tag == "+":
            result.append(content if content.endswith("\n") else content + "\n" if content else "\n")
    result.extend(lines[src_idx:])
    return result


def apply_hunks_to_text(base_text: str, hunks: list[_Hunk]) -> str:
    lines = _split_lines(base_text)
    for hunk in sorted(hunks, key=lambda h: h.old_start, reverse=True):
        lines = apply_hunk(lines, hunk)
    return _join_lines(lines)


def _git_merge_file(base: str, ours: str, theirs: str) -> tuple[str, bool] | None:
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base_p = root / "base"
            ours_p = root / "ours"
            theirs_p = root / "theirs"
            base_p.write_text(base, encoding="utf-8")
            ours_p.write_text(ours, encoding="utf-8")
            theirs_p.write_text(theirs, encoding="utf-8")
            proc = subprocess.run(
                ["git", "merge-file", "-p", "--zdiff3", str(ours_p), str(base_p), str(theirs_p)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode in (0, 1):
                return proc.stdout, proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git merge-file unavailable: %s", exc)
    return None


def _python_merge3(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    """Line-based 3-way merge fallback when git is unavailable."""
    base_lines = _split_lines(base)
    a_lines = _split_lines(ours)
    b_lines = _split_lines(theirs)

    if ours == theirs:
        return ours, True
    if ours == base:
        return theirs, True
    if theirs == base:
        return ours, True

    sm_a = SequenceMatcher(None, base_lines, a_lines)
    sm_b = SequenceMatcher(None, base_lines, b_lines)
    a_edits = sm_a.get_opcodes()
    b_edits = sm_b.get_opcodes()

    merged: list[str] = []
    conflict = False
    ai = bi = 0

    while ai < len(a_edits) or bi < len(b_edits):
        a_op = a_edits[ai] if ai < len(a_edits) else None
        b_op = b_edits[bi] if bi < len(b_edits) else None

        if a_op and b_op:
            # 双方均有编辑：交由下方统一解包与冲突处理逻辑（见 line 297+）
            pass
        elif a_op:
            tag, i1, i2, j1, j2 = a_op
            merged.extend(base_lines[i1:i2] if tag == "equal" else a_lines[j1:j2])
            ai += 1
            continue
        else:
            tag, i1, i2, j1, j2 = b_op  # type: ignore[misc]
            merged.extend(base_lines[i1:i2] if tag == "equal" else b_lines[j1:j2])
            bi += 1
            continue

        tag_a, i1a, i2a, j1a, j2a = a_op
        tag_b, i1b, i2b, j1b, j2b = b_op

        if tag_a == "equal" and tag_b == "equal" and i1a == i1b:
            merged.extend(base_lines[i1a:i2a])
            ai += 1
            bi += 1
            continue

        if i1a == i1b and tag_a == tag_b == "insert":
            # audit #23：同一 base 位置双方都插入。
            # - 内容相同：非冲突，取其一。
            # - 内容不同：这是真冲突，静默"先 a 后 b"拼接会产生语义错乱的合并结果
            #   （顺序任意、两段都塞进去）。改为标记冲突，交上层(merge 节点)重新生成。
            if a_lines[j1a:j2a] == b_lines[j1b:j2b]:
                merged.extend(a_lines[j1a:j2a])
            else:
                conflict = True
                merged.append("<<<<<<< ours\n")
                merged.extend(a_lines[j1a:j2a])
                merged.append("=======\n")
                merged.extend(b_lines[j1b:j2b])
                merged.append(">>>>>>> theirs\n")
            ai += 1
            bi += 1
            continue

        if tag_a == "equal" and tag_b != "equal" and i1a <= i1b:
            merged.extend(base_lines[i1a:i2a])
            ai += 1
            continue
        if tag_b == "equal" and tag_a != "equal" and i1b <= i1a:
            merged.extend(base_lines[i1b:i2b])
            bi += 1
            continue

        chunk_a = a_lines[j1a:j2a] if tag_a != "equal" else base_lines[i1a:i2a]
        chunk_b = b_lines[j1b:j2b] if tag_b != "equal" else base_lines[i1b:i2b]
        if chunk_a == chunk_b:
            merged.extend(chunk_a)
        else:
            # audit #23：双方不同的修改/插入即冲突。原 insert+insert 分支静默拼接
            # （先 a 后 b）会产生语义错乱的结果，改为统一标记冲突，交上层重新生成。
            conflict = True
            merged.append("<<<<<<< ours\n")
            merged.extend(chunk_a)
            merged.append("=======\n")
            merged.extend(chunk_b)
            merged.append(">>>>>>> theirs\n")
        ai += 1
        bi += 1

    return _join_lines(merged), not conflict


def three_way_merge_text(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    git_result = _git_merge_file(base, ours, theirs)
    if git_result is not None and git_result[1]:
        return git_result
    combined = merge_insert_only_changes(base, ours, theirs)
    if combined is not None:
        return combined, True
    if git_result is not None:
        return git_result
    return _python_merge3(base, ours, theirs)


def _collect_pure_insertions(
    base_lines: list[str], mod_lines: list[str]
) -> list[tuple[int, list[str]]] | None:
    """Return list of (insert_before_index, lines) or None if not insert-only."""
    sm = SequenceMatcher(None, base_lines, mod_lines)
    inserts: list[tuple[int, list[str]]] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            inserts.append((i1, mod_lines[j1:j2]))
        else:
            return None
    return inserts


def _is_aggregate_manifest(file_path: str) -> bool:
    """聚合清单文件(成员/依赖显式列表)——多写者向同一锚点加不同条目应并集而非冲突。
    与 worker.workspace_manifest 覆盖的生态一致：Maven/Gradle/Cargo/Go/.NET。"""
    base = file_path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (
        base == "pom.xml"
        or base in ("settings.gradle", "settings.gradle.kts")
        or base == "cargo.toml"
        or base == "go.work"
        or base.endswith(".sln")
    )


def _is_module_manifest(rel: str) -> bool:
    """<dir>/<manifest> 是否为【模块骨架清单】(定义一个可构建模块)。跨栈通用：
    Maven pom.xml / Gradle build.gradle(.kts) / Cargo.toml / Go go.mod / .NET *.csproj 等。
    与 _is_aggregate_manifest(根聚合清单)区分：此判据认【每模块】的构建清单。"""
    base = rel.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return (
        base == "pom.xml"
        or base in ("build.gradle", "build.gradle.kts")
        or base == "cargo.toml"
        or base == "go.mod"
        or base.endswith((".csproj", ".fsproj", ".vbproj"))
    )


def _diff_target_files(diff: str) -> list[str]:
    """从 unified diff 抽 b/ 侧目标文件（相对路径，去 a//b/ 前缀）。"""
    out: list[str] = []
    for line in (diff or "").splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p in ("/dev/null", ""):
                continue
            if p.startswith(("a/", "b/")):
                p = p[2:]
            out.append(p.replace("\\", "/").lstrip("/"))
    return out


def _top_module_dir(rel: str) -> str | None:
    """相对路径的顶层模块目录：'ruoyi-alarm/src/..' → 'ruoyi-alarm'；根级文件 → None。"""
    p = rel.replace("\\", "/").lstrip("/")
    if "/" not in p:
        return None
    return p.split("/", 1)[0]


def filter_orphan_module_patches(
    subtask_diffs: list[tuple[str, str]],
    base_module_exists,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """#11(c) MERGE 硬门控：剔除【引用了骨架缺失模块的补丁】。

    module-defining 子任务(建 <dir> 的模块清单)不在成功集时，若仍纳入引用该模块的兄弟补丁
    (<dir>/src/**)，合并 patch 会有模块目录文件却无该模块骨架 → git apply / reactor 崩
    (No such file / Child module does not exist)，整包交付死于门口。

    判据：一个顶层模块目录 <dir> 若在本次合并集里有任何补丁触达，但 <dir> 的模块清单
    (pom.xml/build.gradle/Cargo.toml/go.mod/*.csproj) 【既不在本次合并集、也不在 repo base】
    → 该 <dir> 骨架缺失 → 剔除 <dir> 下所有补丁(保其余模块正常交付)，显式记原因。
    根级文件(无模块前缀)永不受影响。返回 (保留的 diffs, {缺失模块: [被剔子任务 id...]})。

    base_module_exists(dir) -> bool：repo base 是否已含该模块骨架清单(历史模块)。
    base_module_exists is None（round21 对抗审计护栏）＝base 路径不可用(project_id 缺失/store 抛错)，
    无法区分【骨架缺失孤儿】与【既有模块】→【跳过过滤】(fail-safe：宁可不剔、由 VERIFY_L2/apply
    护栏兜真问题，也不把既有模块误判孤儿→补丁全剔→误杀交付)。
    """
    if base_module_exists is None:
        return subtask_diffs, {}
    defined: set[str] = set()       # 本次合并集里骨架落盘的模块
    referenced: set[str] = set()    # 被任何补丁触达的模块
    files_by_sid: dict[str, list[str]] = {}
    for sid, diff in subtask_diffs:
        files = _diff_target_files(diff)
        files_by_sid[sid] = files
        for f in files:
            d = _top_module_dir(f)
            if d is None:
                continue
            referenced.add(d)
            if _is_module_manifest(f):
                defined.add(d)  # <dir>/pom.xml 等 → 骨架落盘
    orphan_dirs: set[str] = set()
    for d in referenced:
        if d in defined:
            continue
        try:
            if base_module_exists(d):
                continue
        except Exception:  # noqa: BLE001 —— base 探测失败保守视为不存在(fail-closed)
            pass
        orphan_dirs.add(d)
    if not orphan_dirs:
        return subtask_diffs, {}
    filtered: list[tuple[str, str]] = []
    dropped: dict[str, list[str]] = {}
    for sid, diff in subtask_diffs:
        files = files_by_sid.get(sid, [])
        # 该补丁触达的孤儿模块（补丁全部文件都落在骨架缺失的模块目录 → 整条剔除）
        hit = {d for f in files if (d := _top_module_dir(f)) in orphan_dirs}
        if hit and all(_top_module_dir(f) in orphan_dirs for f in files):
            for d in hit:
                dropped.setdefault(d, []).append(sid)
            continue
        filtered.append((sid, diff))
    return filtered, dropped


def merge_insert_only_changes(
    base: str, *branches: str, allow_anchor_union: bool = False
) -> str | None:
    """When every branch only inserts lines relative to base, combine all inserts.

    ``allow_anchor_union``（杠杆A·round9 治本）：聚合清单文件(pom <modules>/<dependencies>、
    settings.gradle include、Cargo members …)里【多写者向同一锚点各插入不同条目】(各加不同
    <module>/<dependency>)是 order-independent 且都该保留——对它们做【并集】(去重相同块、拼接
    不同块)而非拒绝当冲突。仅对聚合清单开启；普通代码仍保守拒绝(同锚点不同插入=真冲突)。
    """
    base_lines = _split_lines(base)
    grouped: dict[int, list[list[str]]] = {}

    for branch in branches:
        if branch == base:
            continue
        mod_lines = _split_lines(branch)
        inserts = _collect_pure_insertions(base_lines, mod_lines)
        if inserts is None:
            return None
        for pos, lines in inserts:
            grouped.setdefault(pos, []).append(lines)

    if not grouped:
        return base

    # A-P1-26 (a)：同一锚点(anchor)多个分支各插入不同内容，绝非"clean"——之前直接把
    # 各 chunk 顺序拼接(AAA 后接 BBB)，等于无声吞掉冲突；且同一内容会被重复插两遍(XXX XXX)。
    # 现：同锚点若插入内容不一致 → 返回 None，交给上层 3-way / 硬冲突路径处理；
    #     若完全一致 → 去重只留一份。不同锚点彼此独立、照常合并。
    # 杠杆A 例外：聚合清单文件(allow_anchor_union)同锚点不同插入 → 并集保留(去重相同块)。
    deduped: dict[int, list[str]] = {}
    for pos, chunks in grouped.items():
        first = chunks[0]
        if all(other == first for other in chunks[1:]):
            deduped[pos] = first  # 同内容去重，只保留一份
        elif allow_anchor_union:
            # 聚合清单同锚点不同插入：去重相同块后按出现序拼接(各 <module>/<dependency> 并存)
            seen: list[list[str]] = []
            for ch in chunks:
                if ch not in seen:
                    seen.append(ch)
            deduped[pos] = [ln for ch in seen for ln in ch]
        else:
            return None  # 普通代码同锚点冲突插入，拒绝当 clean

    result: list[str] = []
    for idx, line in enumerate(base_lines):
        if idx in deduped:
            result.extend(deduped[idx])
        result.append(line)
    if len(base_lines) in deduped:
        result.extend(deduped[len(base_lines)])

    return _join_lines(result)


def _lines_to_unified_diff(file_path: str, base: str, merged: str) -> str:
    import difflib

    # ── 关键治本(996db614 round16 第2289行损坏)：difflib unified_diff 的正确用法 ──
    # 旧代码 `splitlines(keepends=True)`(内容行自带\n) + `lineterm=""` + `"\n".join` → 给本已
    # 含\n的内容行再加\n（行尾翻倍），每行后多一个空行 → hunk 头声明行数与实际不符 → git 解析越界
    # 撞下一个文件头 → "补丁损坏"。照搬 worker/executor.py:1870-1891 已验证的 normalize：先归一
    # 行尾，keepends=True + lineterm="" + 逐元素补换行 + "".join（内容行用自带\n，头部行补\n）。
    base_norm = base.replace("\r\n", "\n").replace("\r", "\n")
    merged_norm = merged.replace("\r\n", "\n").replace("\r", "\n")
    if base_norm == merged_norm:
        return ""
    ud = difflib.unified_diff(
        base_norm.splitlines(keepends=True),
        merged_norm.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    )
    # 逐元素规范化：hunk头/文件头(lineterm="" 故无换行)补\n；内容行(keepends 已含\n)不动 → 无行尾翻倍。
    block = "".join(x if x.endswith("\n") else x + "\n" for x in ud)
    block = block.rstrip("\n")
    if not block.strip():
        return ""
    # round29 B 治本：union/3-way 消解出的 modify 段同样必须带 `diff --git` 头（同 _format_file_patch
    # 非新建分支），否则混排进 git 格式 new-file 段之间会使 git 解析 desync（见该处注释）。
    return f"diff --git a/{file_path} b/{file_path}\n{block}"


def _aggregate_merge_duplicated_singleton(
    base: str, versions: list[str], merged: str
) -> str | None:
    """round18 P0-A 治本：检测聚合清单(根 pom)的 3-way/union 合并是否【伪造了重复的结构单例行】。

    不变量：一行若在 base 与【每个】分支里都至多出现一次（结构单例，如 </modules>/
    </dependencyManagement>/<packaging>pom</packaging>），合并结果里也绝不该出现 >1 次——出现
    即线性 3-way 把两份【各自整段结构重写】的分支背靠背拼接了（对 EOF 前双插入的失败模式），
    产出闭标签/<modules> 块重复的畸形 pom → git apply「补丁未应用」→ 整包连坐回滚（MERGE#2 现场，
    dump merged_diff_996db614_1782973787.diff:57-98）。返回首个被重复的单例行(诊断用)，无则 None。

    普通【可重复行】(</dependency>/</module>/空行/<groupId>…)合法多次出现、不在单例集，不受影响
    → per-entry 并集(各加不同 <module>/<dependency>)不误伤。跨栈通用：纯行计数，无 pom/xml 写死。
    """
    from collections import Counter

    def _counts(t: str) -> Counter:
        return Counter(ln.strip() for ln in _split_lines(t) if ln.strip())

    base_c = _counts(base)
    branch_cs = [_counts(v) for v in versions]
    merged_c = _counts(merged)
    for line, m in merged_c.items():
        if m <= 1:
            continue
        if base_c.get(line, 0) <= 1 and all(bc.get(line, 0) <= 1 for bc in branch_cs):
            return line
    return None


def _try_three_way_resolve(
    file_path: str,
    conflict_hunks: list[_Hunk],
    all_hunks: list[_Hunk],
    base_reader: BaseReader,
) -> tuple[str | None, bool]:
    """Return (unified_diff_from_base, resolved) or (None, False)."""
    base_raw = base_reader(file_path)
    if base_raw is None:
        return None, False

    by_subtask: dict[str, list[_Hunk]] = {}
    for h in all_hunks:
        by_subtask.setdefault(h.subtask_id, []).append(h)

    conflict_ids = list(dict.fromkeys(h.subtask_id for h in conflict_hunks))
    if len(conflict_ids) < 2:
        return None, False

    # D04 治本：3-way 折叠必须覆盖该文件【全部】写者（含只在非冲突锚点改动的第三写者 C），
    # 而非只折叠冲突参与者 {A,B}——否则 C 的 hunk 被静默丢弃（well-formed 骗过 apply/L2）。
    # all_ids 取自 all_hunks（=该文件所有 hunk），按出现序（子任务派发序）确定性排列。
    subtask_ids = list(dict.fromkeys(h.subtask_id for h in all_hunks))

    versions: dict[str, str] = {}
    for sid, hunks in by_subtask.items():
        try:
            versions[sid] = apply_hunks_to_text(base_raw, hunks)
        except HunkContextMismatch as exc:
            # D9 fail-closed：基线漂移的 hunk 绝不喂 3-way（会产语义损坏的"干净"合并）——
            # 放弃自动折叠，调用方落冲突/rebase 重生成。
            logger.warning("[MERGE] D9 hunk 基线漂移（%s，sid=%s）→ 放弃 3-way 落冲突/rebase: %s",
                           file_path, sid, exc)
            return None, False

    is_agg = _is_aggregate_manifest(file_path)
    combined = merge_insert_only_changes(
        base_raw, *versions.values(),
        allow_anchor_union=is_agg,
    )
    if combined is not None and combined != base_raw:
        # round18 P0-A：并集也可能对【非纯插入】残片拼出重复单例；伪造重复 → 拒绝，落 rebase。
        if not (is_agg and _aggregate_merge_duplicated_singleton(
            base_raw, list(versions.values()), combined
        )):
            diff = _lines_to_unified_diff(file_path, base_raw, combined)
            if diff:
                return diff, True

    sid_a, sid_b = subtask_ids[0], subtask_ids[1]
    merged_text, ok = three_way_merge_text(base_raw, versions[sid_a], versions[sid_b])

    # 链式三方折叠累加 >2 个子任务的改动。base 必须始终保持 base_raw（=HEAD，
    # 所有 versions[sid] 唯一的共同祖先），绝不能"演进"成上一轮的 merged_text：
    # 每次 three_way_merge_text(base_raw, merged_text, versions[C]) 等价于
    # git merge-file ours=merged_text base=base_raw theirs=C，三方算法据共同祖先
    # base_raw 算出 C 相对祖先的改动并叠加到 merged_text，正确累加 base+A+B+C。
    # 若把 base 换成 merged_text(已含 A+B)，C 相对它的 diff 会把 A/B 的新增内容
    # 误判为"需删除"，从而丢掉更早分支的改动——这是回归，故 base 固定不变。
    # 特征化保护见 test/test_merge_engine_nway.py。
    if len(subtask_ids) > 2:
        for sid in subtask_ids[2:]:
            merged_text, ok2 = three_way_merge_text(base_raw, merged_text, versions[sid])
            ok = ok and ok2

    if "<<<<<<" in merged_text or ">>>>>>>" in merged_text:
        return None, False

    # round18 P0-A 治本：聚合清单 3-way 若伪造了重复的结构单例（两份整段结构重写背靠背拼接），
    # 产出的是畸形 pom（git apply 必败、整包连坐）。拒绝此"假消解"→ 返回 (None,False) 让上层
    # 落 rebase（保留拓扑上游写者的干净版、下游标记重生成，即 MERGE#1 成功的 rebase=5 路径）。
    if is_agg:
        dup = _aggregate_merge_duplicated_singleton(
            base_raw, list(versions.values()), merged_text
        )
        if dup is not None:
            logger.warning(
                "[MERGE] 聚合清单 %s 3-way 合并伪造重复结构单例 %r（双写者整段重写拼接）"
                "→ 拒绝自动消解，改走 rebase（保留上游写者干净版）",
                file_path, dup,
            )
            return None, False

    diff = _lines_to_unified_diff(file_path, base_raw, merged_text)
    return diff, ok


def merge_diffs(
    subtask_diffs: list[tuple[str, str]],
    *,
    base_reader: BaseReader | None = None,
    auto_resolve: bool = True,
    subtask_order: list[str] | None = None,
) -> MergeResult:
    """Merge unified diffs from multiple subtasks.

    When ``base_reader`` is provided and ``auto_resolve`` is True, overlapping
    hunks are resolved via 3-way merge (git merge-file or Python fallback).

    ``subtask_order``：子任务 ID 的依赖拓扑序（被依赖者在前）。rebase 策略据此选 base
    （A-P1-26c）——以依赖【上游】为 base 保留其 diff，把【下游】依赖者标记 rebase 重生成；
    缺省 None 时退回旧行为（按冲突 hunk 在文件中的出现序选第一个为 base）。
    """
    if not subtask_diffs:
        return MergeResult(merged_diff="", conflicts=[], success=True)

    # 拓扑优先级：值越小越上游（越该当 base）。缺省/缺失 ID 退回出现序兜底。
    order_prio: dict[str, int] = {
        sid: idx for idx, sid in enumerate(subtask_order or [])
    }

    by_file: dict[str, list[_Hunk]] = {}
    headers: dict[str, list[str]] = {}
    deletion_files: set[str] = set()               # D03：整段删除意图的文件
    passthrough_parts: list[str] = []              # D06：rename/binary 段原样透传
    seen_passthrough: set[tuple[str, str]] = set()  # (file_path, raw) 去重相同透传段

    for subtask_id, diff in subtask_diffs:
        if not (diff or "").strip():
            continue
        for raw_chunk in _split_raw_diffs(diff):
            patch = _parse_file_patch(raw_chunk, subtask_id)
            if patch is None:
                continue
            # D06：rename/binary 无法字符级合并 → 整段透传（去重相同段避免重复 apply）。
            if patch.passthrough is not None:
                key = (patch.file_path, patch.passthrough)
                if key not in seen_passthrough:
                    seen_passthrough.add(key)
                    passthrough_parts.append(patch.passthrough)
                    logger.info(
                        "[MERGE] 段透传保留（rename/binary，不可字符级合并）: %s（子任务 %s）",
                        patch.file_path, subtask_id,
                    )
                continue
            by_file.setdefault(patch.file_path, [])
            if patch.file_path not in headers and patch.header_lines:
                headers[patch.file_path] = patch.header_lines
            by_file[patch.file_path].extend(patch.hunks)
            if patch.is_deletion:
                deletion_files.add(patch.file_path)  # D03：标记删除意图

    if not by_file and not passthrough_parts:
        return MergeResult(merged_diff="", conflicts=[], success=True)

    conflicts: list[MergeConflict] = []
    auto_resolved_files: list[str] = []
    rebase_subtask_ids_all: list[str] = []   # 全局累加需要 rebase 重生成的子任务 ID
    rebase_origin_all: dict[str, str] = {}   # 6.9-HF3：sid → "new_file"|"three_way"（终点分流）
    conflict_render_parts: list[str] = []    # D11：硬冲突渲染（诊断专用，绝不进 merged_diff）
    merged_parts: list[str] = []

    for file_path in sorted(by_file.keys()):
        hunks = by_file[file_path]

        # ── D03 删除专路（最先判定）──
        # 删除段的 hunk 是全 `-` 行；须输出 `+++ /dev/null` 删除补丁而非被当 modify/新文件蒸发。
        # hunter#1 复核整改：专路不得对多写者盲拼 hunks——那会绕过整个冲突机制：
        #   (a) 双删除（良性一致意图）拼出重复删除 hunk → git apply 必败；
        #   (b) 删除 vs 修改（真协同冲突）把 `+` 行拼进 `+++ /dev/null` 段
        #       → `removed file still has content`，且被误标"组装缺陷"误导排障。
        if file_path in deletion_files:
            # 按 hunk 形态划分：纯删除 hunk（body 全 `-`）vs 修改 hunk（含 `+`/context）。
            del_hunks: list[_Hunk] = []
            mod_hunks: list[_Hunk] = []
            for h in hunks:
                body = [ln for ln in (h.lines[1:] if h.lines else []) if ln != ""]
                if body and all(ln.startswith("-") for ln in body):
                    del_hunks.append(h)
                else:
                    mod_hunks.append(h)
            if mod_hunks:
                # 真实 delete-vs-modify 协同冲突：如实上报 MergeConflict 交下游冲突机制
                # （rebase/replan/人工），绝不产非法补丁、绝不静默偏袒任一方。
                all_writers = sorted({h.subtask_id for h in hunks})
                logger.warning(
                    "[MERGE] delete-vs-modify 冲突: 文件 %s 同时被删除(%s)与修改(%s)，"
                    "如实上报冲突（不拼接非法删除补丁）",
                    file_path,
                    sorted({h.subtask_id for h in del_hunks}),
                    sorted({h.subtask_id for h in mod_hunks}),
                )
                conflicts.append(
                    MergeConflict(
                        file_path=file_path,
                        subtask_ids=all_writers,
                        message=(
                            f"delete-vs-modify conflict in {file_path}: "
                            f"deleted by {sorted({h.subtask_id for h in del_hunks})}, "
                            f"modified by {sorted({h.subtask_id for h in mod_hunks})}"
                        ),
                    )
                )
                continue
            # 全部为删除意图：按 (old_start, body) 去重——N 个子任务一致删除同一文件是
            # 良性共识，合成单份删除补丁；不同 range 的删除 hunk（分段删除）保留各自一份。
            seen_del: set[tuple[int, tuple[str, ...]]] = set()
            uniq_del: list[_Hunk] = []
            for h in sorted(del_hunks, key=lambda h: h.old_start):
                key = (h.old_start, tuple(h.lines[1:] if h.lines else []))
                if key in seen_del:
                    continue
                seen_del.add(key)
                uniq_del.append(h)
            # 同 old_start 不同 body = 两写者对同一文件内容认知不一致（基线分叉），
            # 去重救不了（重复 range 补丁仍非法）→ 如实报冲突，fail-closed。
            starts = [h.old_start for h in uniq_del]
            if len(starts) != len(set(starts)):
                all_writers = sorted({h.subtask_id for h in del_hunks})
                logger.warning(
                    "[MERGE] 删除文件 %s 多写者 %s 的删除 hunk 同 range 不同内容"
                    "（基线认知分叉），如实上报冲突", file_path, all_writers,
                )
                conflicts.append(
                    MergeConflict(
                        file_path=file_path,
                        subtask_ids=all_writers,
                        message=(f"divergent deletion hunks for {file_path} "
                                 f"from {', '.join(all_writers)}"),
                    )
                )
                continue
            writers = sorted({h.subtask_id for h in del_hunks})
            if len(writers) > 1:
                logger.info(
                    "[MERGE] 删除文件 %s 多写者 %s 意图一致，删除 hunk 去重合成单份补丁",
                    file_path, writers,
                )
            merged_parts.append(_format_deletion_patch(file_path, uniq_del))
            continue

        # 新/旧由 merge base 权威判定（非 worker 头）：有 base_reader 且它读不到该文件 = 新文件。
        # base_reader 缺省时保守判 modify（旧行为，不回归）。round17 根因②治本。
        is_new_file = base_reader is not None and base_reader(file_path) is None

        # ── 新文件专路（base 无此文件）──
        # base 不存在 → 3-way/union/rebase 都无从谈起（都需读 base 内容）。多写者(同一新文件多个
        # 子任务)若走下方通用路：非冲突会被 _format_file_patch 把两份内容【拼接翻倍】，冲突会 emit
        # 【冲突标记】(不可 apply，毒化整包 → round17 sdk pom 多写者实测 apply 失败)。故这里专门处理：
        #   内容一致 → 去重取一；不一致 → 确定性取拓扑最上游写者(同 rebase 选 base 逻辑)，其余记录丢弃。
        # 绝不 emit 冲突标记（新文件无"冲突"可言）。单写者直接输出。
        if is_new_file:
            by_sid_new: dict[str, list[_Hunk]] = {}
            for h in hunks:
                by_sid_new.setdefault(h.subtask_id, []).append(h)
            if len(by_sid_new) >= 2:
                bodies = {sid: _new_side_lines(sh) for sid, sh in by_sid_new.items()}
                distinct = list({tuple(v) for v in bodies.values()})
                if len(distinct) == 1:
                    chosen_sid = next(iter(by_sid_new))            # 全一致 → 取一
                else:
                    chosen_sid = min(                              # 不一致 → 拓扑最上游
                        by_sid_new,
                        key=lambda s: order_prio.get(s, len(order_prio) + list(by_sid_new).index(s)),
                    )
                    _dropped_new_sids = [s for s in by_sid_new if s != chosen_sid]
                    # D2（阶段6，登记册 §五）：非选中写者不再静默丢——并入 rebase 通道
                    # （merge 后该文件已在树，重派 worker 在其上重生成；账面不再假成功）。
                    rebase_subtask_ids_all.extend(_dropped_new_sids)
                    # 6.9-HF3：标记来源=new_file（选中版已交付，超限终点走 abandoned 非 escalate）
                    for _ds in _dropped_new_sids:
                        rebase_origin_all.setdefault(_ds, "new_file")
                    logger.warning(
                        "[MERGE] 新文件 %s 多写者内容不一致，确定性取 %s，其余 %s 进 rebase"
                        "重生成（D2：不再静默丢弃）",
                        file_path, chosen_sid, _dropped_new_sids,
                    )
                chosen_hunks = by_sid_new[chosen_sid]
            else:
                chosen_hunks = hunks
            merged_parts.append(
                _format_file_patch(file_path, headers.get(file_path, []), chosen_hunks, is_new=True,
                                   base_known=base_reader is not None)
            )
            continue

        conflicting: set[int] = set()

        for i in range(len(hunks)):
            for j in range(i + 1, len(hunks)):
                if hunks[i].subtask_id != hunks[j].subtask_id and hunks[i].overlaps(hunks[j]):
                    conflicting.add(i)
                    conflicting.add(j)

        if conflicting:
            conflict_hunks = [hunks[i] for i in sorted(conflicting)]
            subtask_ids = list(dict.fromkeys(h.subtask_id for h in conflict_hunks))
            resolved_diff: str | None = None

            if auto_resolve and base_reader is not None:
                resolved_diff, resolved = _try_three_way_resolve(
                    file_path, conflict_hunks, hunks, base_reader
                )
                if resolved and resolved_diff:
                    auto_resolved_files.append(file_path)
                    # resolved_diff（union）已含该文件【全部】子任务的插入（含非冲突锚点）——
                    # 见 _try_three_way_resolve: by_subtask 建自 all_hunks，versions[sid]=base+该子任务
                    # 全部 hunk，merge_insert_only_changes 并集所有 version。故此处【绝不能】再 append
                    # non_conflicting：否则同一插入进两个块，git apply 累积应用 → 块2 用 base 原始行号
                    # 在被块1 改过的镜像上错位 →「补丁未应用」(round17 pom.xml:215 根因①，复现坐实)。
                    merged_parts.append(resolved_diff)
                    continue

            # ── Rebase 重生成策略（3-way 和硬冲突之间的中间档）──
            # 当 3-way 无法自动解决且提供了 base_reader 时：
            #   1. 选【依赖上游】子任务(st-a)的 diff 作为 base 先 apply（A-P1-26c：
            #      依拓扑序而非 hunk 出现序——上游是地基，下游才该 rebase 到其之上）
            #   2. 将其余冲突子任务标记为 rebase（需要基于已含 st-a 变更的最新状态重新生成）
            #   3. 保留 base 方的 diff，不报硬冲突
            # 前提: base_reader 能读取该文件的内容（否则无法构建 base 版本，走硬冲突）
            # 如果没有 base_reader 或只有 1 个子任务参与冲突，走原有硬冲突路径
            if (
                auto_resolve
                and base_reader is not None
                and base_reader(file_path) is not None
                and len(subtask_ids) >= 2
            ):
                # 选最上游(拓扑序最小)的冲突子任务为 base；无 order 时退回出现序首个。
                # min 对相等键返回首个遇到者(=subtask_ids 出现序)，故缺序时与旧行为一致。
                base_sid = min(
                    subtask_ids,
                    key=lambda s: order_prio.get(s, len(order_prio) + subtask_ids.index(s)),
                )
                rebase_sids = [s for s in subtask_ids if s != base_sid]
                # 收集 base 方的冲突 hunk（保留到合并结果）
                base_conflict_hunks = [h for h in conflict_hunks if h.subtask_id == base_sid]
                # 收集所有非冲突 hunk
                non_conflicting = [h for i, h in enumerate(hunks) if i not in conflicting]
                # 合并: base 方冲突 hunk + 非冲突 hunk
                kept_hunks = non_conflicting + base_conflict_hunks
                merged_parts.append(
                    _format_file_patch(file_path, headers.get(file_path, []), kept_hunks, is_new_file,
                                       base_known=base_reader is not None)
                )
                # 记录 rebase 子任务
                rebase_subtask_ids_all.extend(rebase_sids)
                for _rs in rebase_sids:  # 6.9-HF3：真源码 hunk 被丢 → three_way（超限走 escalate）
                    rebase_origin_all[_rs] = "three_way"
                logger.info(
                    "[MERGE] rebase 策略: 文件 %s, 保留 %s 的 diff, 标记 %s 待 rebase 重生成",
                    file_path, base_sid, rebase_sids,
                )
                continue

            # ── 硬冲突兜底（无 base_reader 或仅单子任务冲突）──
            logger.warning(
                "[MERGE] 硬冲突升级: 文件 %s, 子任务 %s 存在重叠/同锚点不同插入 hunk，"
                "无法自动合并，落入冲突标记（需人工或 rebase 重生成）",
                file_path, ", ".join(subtask_ids),
            )
            conflicts.append(
                MergeConflict(
                    file_path=file_path,
                    subtask_ids=subtask_ids,
                    message=f"overlapping hunks in {file_path} from {', '.join(subtask_ids)}",
                )
            )
            # D11（阶段6，登记册 §五）：冲突标记（<<<<<<<）绝不写进 merged_diff——
            # 毒 diff 不可 apply 且与同文件非冲突段双段互踩；单独落 conflict_render
            # 供诊断件（merge 节点 dump），merged_diff 保持可 apply。
            conflict_render_parts.append(
                _format_conflict_hunks(file_path, conflict_hunks, is_new_file))
            non_conflicting = [h for i, h in enumerate(hunks) if i not in conflicting]
            if non_conflicting:
                merged_parts.append(
                    _format_file_patch(file_path, headers.get(file_path, []), non_conflicting, is_new_file,
                                       base_known=base_reader is not None)
                )
        else:
            merged_parts.append(
                _format_file_patch(file_path, headers.get(file_path, []), hunks, is_new_file,
                                       base_known=base_reader is not None)
            )

    # D06：rename/binary 透传段追加到输出末尾（每段自带 `diff --git` 头，头数==段数不变量成立）。
    merged_parts.extend(passthrough_parts)
    merged_diff = "\n\n".join(p for p in merged_parts if p.strip())
    # 有 rebase 子任务时不算硬冲突成功，但也不走硬冲突路径
    # success = True 允许继续走 verify_l2; rebase 子任务会被 merge() 节点
    # 重新放回 dispatch_remaining 进行重生成
    return MergeResult(
        merged_diff=merged_diff,
        conflicts=conflicts,
        success=len(conflicts) == 0,
        auto_resolved_files=auto_resolved_files,
        rebase_subtask_ids=list(dict.fromkeys(rebase_subtask_ids_all)),
        conflict_render="\n\n".join(conflict_render_parts),
        rebase_origin=dict(rebase_origin_all),
    )


def dump_merged_diff_for_diagnosis(
    task_id: str, merged_diff: str, dump_dir: str = "logs_archive/process", ts: int | None = None
) -> str | None:
    """Fix 0（round17）：apply 失败时把 merged_diff 完整落盘供离线定位组装缺陷。

    verify_merged_patch_applies 用 delete=True 临时文件跑完即删，merged diff 从不落盘 → 每轮
    只能靠 agent 逆推。此 helper 在 MERGE apply_ok=False 时被调用落盘。**fail-safe**：任何异常
    都吞掉返回 None，绝不影响主流程。ts 可注入以便测试确定性。返回落盘路径或 None。
    """
    try:
        import time as _time
        tid8 = (task_id or "task")[:8]
        stamp = ts if ts is not None else int(_time.time())
        d = Path(dump_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"merged_diff_{tid8}_{stamp}.diff"
        # round29 反误诊：git 要求补丁文件以换行结尾，否则末行报 "corrupt patch at line N"。
        # 生产两条 apply 路径(verify_merged_patch_applies / project.diff_apply)都在写临时文件时补尾
        # 换行，但 merged_diff 本体("\n".join 组装)不带——dump 原样落盘会让离线手工 `git apply` 在
        # EOF 假报损坏（task d37a52a3 实证：79 段补丁被误诊为"组装畸形"，实际补尾换行即干净 apply）。
        # 落盘对齐生产 apply 的真实输入，dump 文件开箱即可复现。
        if merged_diff and not merged_diff.endswith("\n"):
            merged_diff += "\n"
        p.write_text(merged_diff, encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001
        return None


def _apply_check_against_base(project_path: str, diff_path: str, base_ref: str) -> tuple[bool, str]:
    """把 merged_diff `git apply --check` 到【纯净 base 树】而非脏工作树。

    ★round29 治本（task d37a52a3 实测，77 文件全中）★：pull-back 把已完成子任务产物 materialize
    进工作区后，工作树里已有 merged_diff 中本应"新建"的文件（ruoyi-alarm/**）。原先直接
    `git apply --check`（默认校验【工作树】）→ create 补丁撞"already exists in working directory"
    → apply_ok=False → fail-closed 升级人工 → 本应 PARTIAL 的任务被打成 FAILED。但 merged_diff 由
    base_reader 相对 git HEAD/钉扎 base 生成（round21，nodes:1371）——生成基线与校验基线分了叉才是
    真死因。治本：校验必须与生成同源，对 base 树校验。

    做法：用【临时 index】从 base_ref `read-tree` 载入基线树，再 `git apply --check --cached` 只对该
    index 校验，完全不碰工作树/暂存区（GIT_INDEX_FILE 指向临时文件，真 index 零改动）。这样：
    · create 补丁 → base 树无此文件 → 通过（工作树是否已有由 pull-back 决定，与"补丁是否合法"无关）；
    · modify 补丁 → context 对 base blob 校验（与 worker 相对 base 生成的 hunk 同源），工作树脏不误判；
    · 畸形补丁 → 仍 well-formed 校验失败（fail-closed 不放水）。

    base_ref 不可达（GC/坏 SHA）→ 退回 HEAD 树；HEAD 也 read-tree 失败（极端损坏仓）→ 退回旧的工作树
    `git apply --check`（不回归 greenfield，宁可保守）。
    """
    with tempfile.TemporaryDirectory(prefix="mergeidx_") as _td:
        idx = os.path.join(_td, "index")
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        # 临时 index 载入 base 树（不动真 index/工作树）。base_ref 坏 → 退回 HEAD。
        loaded = base_ref
        rt = subprocess.run(
            ["git", "read-tree", base_ref], cwd=project_path, env=env,
            capture_output=True, text=True, timeout=30,
        )
        _readtree_err = (rt.stderr or "").strip()  # 保住首次 read-tree 失败真因（供 finding2 诊断）
        if rt.returncode != 0 and base_ref != "HEAD":
            # 复核 finding1（观测·降级可观测红线）：钉扎 base 不可达退回 HEAD 是【换了校验基线】。
            # 若同时 HEAD 已漂移（worktree_diverged_from_base / 3rd-P1b 担心的场景），成功路径也会对
            # 【与 diff 不同源的树】判 ok=True——必须在替换发生的当下 loud，否则成功轮完全无痕迹。
            logger.warning(
                "[MERGE] apply-check 钉扎 base %s 不可达(GC/坏 ref?)，退回 HEAD 校验——"
                "若 HEAD 已漂移则校验基线与 diff 生成基线不同源: %s",
                base_ref[:12], _readtree_err or "read-tree 非零退出",
            )
            loaded = "HEAD"
            rt = subprocess.run(
                ["git", "read-tree", "HEAD"], cwd=project_path, env=env,
                capture_output=True, text=True, timeout=30,
            )
            _readtree_err = (rt.stderr or "").strip() or _readtree_err
        # --ignore-whitespace：与真实交付 apply(project/diff_apply.apply_git_diff:168) 同旗标——RuoYi 等
        # 项目源文件 CRLF、worker 产出 diff LF，不忽略行尾差异会让 check 因 context CRLF↔LF 不匹配假失败、
        # 而真 apply 却能过 → check 必须与真 apply 同源，才是忠实预测器(不假红误升级、不假绿放行)。
        if rt.returncode != 0:
            # 连 HEAD 都载不进（空仓/损坏）→ 退回旧的工作树校验，绝不假绿放行。复核 finding2：带上
            # read-tree 失败真因，否则升级人工只看到下游"already exists"症状、误判是补丁问题而非坏 base。
            proc = subprocess.run(
                ["git", "apply", "--check", "--ignore-whitespace", diff_path],
                cwd=project_path, capture_output=True, text=True, timeout=60,
            )
            if proc.returncode == 0:
                return True, ""
            _apply_err = (proc.stderr or proc.stdout or "git apply --check failed").strip()
            _prefix = f"[base 树 read-tree 失败: {_readtree_err}] " if _readtree_err else ""
            return False, f"{_prefix}{_apply_err}"

        proc = subprocess.run(
            ["git", "apply", "--check", "--cached", "--ignore-whitespace", diff_path],
            cwd=project_path, env=env, capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0:
            return True, ""
        _err = (proc.stderr or proc.stdout or "git apply --check --cached failed").strip()
        return False, f"[base={loaded}] {_err}"


def verify_merged_patch_applies(
    project_path: str | None, merged_diff: str, base_ref: str = "HEAD",
) -> tuple[bool, str]:
    """交付前 fail-closed 护栏：合并 patch 能否干净 apply 到项目【纯净 base 树】（非脏工作树）。

    返回 (ok, detail)。ok=False = merge 组装出了【不可 apply 的补丁】（确定性组装缺陷），绝不能
    静默按 success 放行——round16 实测 88 个新文件 `@@ -0,1` + 行尾翻倍空行 → 补丁损坏，却仍
    success=True 蒙混到 VERIFY_L2 才被拦、进而触发全量 replan。此护栏在 MERGE 出口就诚实标注。

    base_ref＝任务钉扎的 base commit（resolve_base_ref 解析，默认 "HEAD"）。校验对齐 diff 的生成基线
    （round29 治本，见 _apply_check_against_base），避免 pull-back 污染工作树后 create 补丁被误判
    "already exists"。老调用点不传 base_ref → 默认 HEAD，同样受益于 base 树校验、零回归。

    空 diff / 无 git 工作树 → ok=True（无可校验、不误报）。P1-2 治本：校验器【自身异常】过去
    fail-open 返回 ok=True——但 #1(a) 起 `_apply_ok=False` 会升级人工，异常仍 True 等于"校验器崩了
    却当补丁可 apply"，是纵深漏洞。改 fail-closed：无法完成 apply-check（异常/超时）→ ok=False，
    宁可升级人工复核，绝不在【无法确认可落盘】时放行。detail 明确区分"未能执行"与"patch 损坏"。
    """
    if not merged_diff.strip():
        return True, ""
    if not project_path or not Path(project_path, ".git").exists():
        return True, ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=True) as tf:
            tf.write(merged_diff if merged_diff.endswith("\n") else merged_diff + "\n")
            tf.flush()
            return _apply_check_against_base(project_path, tf.name, base_ref or "HEAD")
    except Exception as exc:  # noqa: BLE001
        # fail-closed：无法执行校验 → 不冒充可 apply（避免 #1(a) escalate 被 fail-open 绕过）。
        return False, f"apply-check 未能执行(fail-closed，无法确认可落盘): {exc}"
