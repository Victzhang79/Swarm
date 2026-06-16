"""Merge engine — parse unified diffs, 3-way auto-resolve, conflict detection."""

from __future__ import annotations

import logging
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


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _parse_int(value: str | None, default: int = 1) -> int:
    if value is None or value == "":
        return default
    return int(value)


def _split_raw_diffs(diff: str) -> list[str]:
    """Split a multi-file unified diff into per-file chunks."""
    text = diff.strip()
    if not text:
        return []

    lines = text.splitlines(keepends=True)
    chunks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        is_new_file = line.startswith("diff --git ") or (
            line.startswith("--- ") and current and any(l.startswith("+++ ") for l in current)
        )
        if is_new_file and current:
            chunks.append(current)
            current = [line]
        else:
            current.append(line)

    if current:
        chunks.append(current)

    return ["".join(c).strip() for c in chunks if "".join(c).strip()]


def _parse_file_patch(raw: str, subtask_id: str) -> _FilePatch | None:
    lines = raw.splitlines()
    if not lines:
        return None

    file_path = ""
    header_lines: list[str] = []
    hunks: list[_Hunk] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("--- "):
            header_lines.append(line)
            i += 1
            if i < len(lines) and lines[i].startswith("+++ "):
                plus = lines[i]
                header_lines.append(plus)
                path = plus[6:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                if path and path != "/dev/null":
                    file_path = path
                i += 1
            continue

        match = _HUNK_RE.match(line)
        if match:
            hunk_lines = [line]
            i += 1
            while i < len(lines) and not lines[i].startswith("@@ ") and not lines[i].startswith("--- "):
                hunk_lines.append(lines[i])
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
        file_path = "unknown"

    return _FilePatch(file_path=file_path, header_lines=header_lines, hunks=hunks)


def _format_file_patch(file_path: str, header_lines: list[str], hunks: list[_Hunk]) -> str:
    if header_lines:
        header = list(header_lines)
        if header[0].startswith("--- "):
            header[0] = f"--- a/{file_path}"
        if len(header) > 1 and header[1].startswith("+++ "):
            header[1] = f"+++ b/{file_path}"
    else:
        header = [f"--- a/{file_path}", f"+++ b/{file_path}"]

    body: list[str] = []
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        body.extend(hunk.lines)

    # hunk.lines 来自 _parse_file_patch 的 raw.splitlines()（不含尾换行），
    # 故用 "\n".join 补换行是正确的。但 body 行可能混入【空字符串】元素
    # （worker 产出的 diff 行尾为 \n\n / CRLF 时，上游 splitlines 留下空行），
    # 直接 join 会把空行变成额外换行 → diff 行尾翻倍 → git apply "补丁损坏"
    # （task 16098179 实证）。这里过滤 body 内的纯空行（diff 正文每行至少有
    # 一个前缀字符 ' '/'+'/'-'/'@'/'\'，真正的空上下文行是 " " 单空格，不会是 ""）。
    body = [ln for ln in body if ln != ""]
    return "\n".join(header + body)


def _format_conflict_hunks(file_path: str, hunks: list[_Hunk]) -> str:
    subtask_ids = list(dict.fromkeys(h.subtask_id for h in hunks))
    parts = [
        f"# ═══ MERGE CONFLICT: {file_path} (subtasks: {', '.join(subtask_ids)}) ═══",
        f"--- a/{file_path}",
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


def apply_hunk(lines: list[str], hunk: _Hunk) -> list[str]:
    """Apply one unified hunk to line list (1-indexed old_start)."""
    idx = max(hunk.old_start - 1, 0)
    result = lines[:idx]
    src_idx = idx
    for raw in hunk.lines[1:]:
        if not raw:
            continue
        if raw.startswith("\\ No newline"):
            continue
        tag = raw[0]
        content = raw[1:] if len(raw) > 1 else ""
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


def merge_insert_only_changes(base: str, *branches: str) -> str | None:
    """When every branch only inserts lines relative to base, combine all inserts."""
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

    result: list[str] = []
    for idx, line in enumerate(base_lines):
        if idx in grouped:
            for chunk in grouped[idx]:
                result.extend(chunk)
        result.append(line)
    if len(base_lines) in grouped:
        for chunk in grouped[len(base_lines)]:
            result.extend(chunk)

    return _join_lines(result)


def _lines_to_unified_diff(file_path: str, base: str, merged: str) -> str:
    import difflib

    base_lines = base.splitlines(keepends=True)
    merged_lines = merged.splitlines(keepends=True)
    if base == merged:
        return ""
    diff_lines = difflib.unified_diff(
        base_lines,
        merged_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    )
    body = list(diff_lines)
    if not body:
        return ""
    return "\n".join(body)


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

    subtask_ids = list(dict.fromkeys(h.subtask_id for h in conflict_hunks))
    if len(subtask_ids) < 2:
        return None, False

    versions: dict[str, str] = {}
    for sid, hunks in by_subtask.items():
        versions[sid] = apply_hunks_to_text(base_raw, hunks)

    combined = merge_insert_only_changes(base_raw, *versions.values())
    if combined is not None and combined != base_raw:
        diff = _lines_to_unified_diff(file_path, base_raw, combined)
        if diff:
            return diff, True

    sid_a, sid_b = subtask_ids[0], subtask_ids[1]
    merged_text, ok = three_way_merge_text(base_raw, versions[sid_a], versions[sid_b])

    if len(subtask_ids) > 2:
        for sid in subtask_ids[2:]:
            merged_text, ok2 = three_way_merge_text(base_raw, merged_text, versions[sid])
            ok = ok and ok2

    if "<<<<<<" in merged_text or ">>>>>>>" in merged_text:
        return None, False

    diff = _lines_to_unified_diff(file_path, base_raw, merged_text)
    return diff, ok


def merge_diffs(
    subtask_diffs: list[tuple[str, str]],
    *,
    base_reader: BaseReader | None = None,
    auto_resolve: bool = True,
) -> MergeResult:
    """Merge unified diffs from multiple subtasks.

    When ``base_reader`` is provided and ``auto_resolve`` is True, overlapping
    hunks are resolved via 3-way merge (git merge-file or Python fallback).
    """
    if not subtask_diffs:
        return MergeResult(merged_diff="", conflicts=[], success=True)

    by_file: dict[str, list[_Hunk]] = {}
    headers: dict[str, list[str]] = {}

    for subtask_id, diff in subtask_diffs:
        if not (diff or "").strip():
            continue
        for raw_chunk in _split_raw_diffs(diff):
            patch = _parse_file_patch(raw_chunk, subtask_id)
            if patch is None:
                continue
            by_file.setdefault(patch.file_path, [])
            if patch.file_path not in headers and patch.header_lines:
                headers[patch.file_path] = patch.header_lines
            by_file[patch.file_path].extend(patch.hunks)

    if not by_file:
        return MergeResult(merged_diff="", conflicts=[], success=True)

    conflicts: list[MergeConflict] = []
    auto_resolved_files: list[str] = []
    rebase_subtask_ids_all: list[str] = []   # 全局累加需要 rebase 重生成的子任务 ID
    merged_parts: list[str] = []

    for file_path in sorted(by_file.keys()):
        hunks = by_file[file_path]
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
                    merged_parts.append(resolved_diff)
                    non_conflicting = [h for i, h in enumerate(hunks) if i not in conflicting]
                    if non_conflicting:
                        merged_parts.append(
                            _format_file_patch(file_path, headers.get(file_path, []), non_conflicting)
                        )
                    continue

            # ── Rebase 重生成策略（3-way 和硬冲突之间的中间档）──
            # 当 3-way 无法自动解决且提供了 base_reader 时：
            #   1. 选第一个子任务(st-a)的 diff 作为 base 先 apply
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
                base_sid = subtask_ids[0]
                rebase_sids = subtask_ids[1:]
                # 收集 base 方的冲突 hunk（保留到合并结果）
                base_conflict_hunks = [h for h in conflict_hunks if h.subtask_id == base_sid]
                # 收集所有非冲突 hunk
                non_conflicting = [h for i, h in enumerate(hunks) if i not in conflicting]
                # 合并: base 方冲突 hunk + 非冲突 hunk
                kept_hunks = non_conflicting + base_conflict_hunks
                merged_parts.append(
                    _format_file_patch(file_path, headers.get(file_path, []), kept_hunks)
                )
                # 记录 rebase 子任务
                rebase_subtask_ids_all.extend(rebase_sids)
                logger.info(
                    "[MERGE] rebase 策略: 文件 %s, 保留 %s 的 diff, 标记 %s 待 rebase 重生成",
                    file_path, base_sid, rebase_sids,
                )
                continue

            # ── 硬冲突兜底（无 base_reader 或仅单子任务冲突）──
            conflicts.append(
                MergeConflict(
                    file_path=file_path,
                    subtask_ids=subtask_ids,
                    message=f"overlapping hunks in {file_path} from {', '.join(subtask_ids)}",
                )
            )
            merged_parts.append(_format_conflict_hunks(file_path, conflict_hunks))
            non_conflicting = [h for i, h in enumerate(hunks) if i not in conflicting]
            if non_conflicting:
                merged_parts.append(
                    _format_file_patch(file_path, headers.get(file_path, []), non_conflicting)
                )
        else:
            merged_parts.append(
                _format_file_patch(file_path, headers.get(file_path, []), hunks)
            )

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
    )
