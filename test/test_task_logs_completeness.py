"""治本：WebUI 任务日志不完整。

read_task_logs 旧逻辑只在【尾部零命中】才回退扫轮转 backup；长跑中任务尾部总有近期命中 →
永不回退 → 早期 ANALYZE/PLAN 日志（被挤出 tail 窗口 / 滚进 swarm.log.1）丢失。
修复：尾部命中【不足 limit】就回退扫全量 + backup，补齐更早行。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from swarm.logging_config import read_task_logs


def _write(p: Path, lines: list[str]) -> None:
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_pulls_early_logs_from_backup_when_tail_below_limit(tmp_path):
    """早期日志在 swarm.log.1，近期在 swarm.log；matched<limit 应回退把两者都取回。"""
    main = tmp_path / "swarm.log"
    bak = tmp_path / "swarm.log.1"
    short = "abcd1234"
    # 备份(更早)：ANALYZE/PLAN 阶段
    _write(bak, [f"[task={short}] [ANALYZE] 早期分析行{i}" for i in range(5)])
    # 主日志(更近)：DISPATCH 阶段 + 一些别的任务噪声
    _write(main, [f"[task={short}] [DISPATCH] 近期派发行{i}" for i in range(3)]
                 + ["[task=other999] 无关任务行"])

    with patch("swarm.logging_config.resolve_log_path", return_value=main):
        lines = read_task_logs("abcd1234ffff", limit=500)

    text = "\n".join(lines)
    assert "[ANALYZE] 早期分析行0" in text, "早期(backup)日志必须被取回——这是修复要点"
    assert "[DISPATCH] 近期派发行2" in text, "近期(主日志)日志也在"
    assert "无关任务行" not in text, "别的任务日志不应混入"
    # 时间顺序：backup(早) 在前，main(近) 在后
    assert text.index("早期分析行0") < text.index("近期派发行0")


def test_tail_only_when_enough_recent_matches(tmp_path):
    """尾部已 ≥limit 命中（纯 live tail）时按 limit 截取最新，不必强扫全量。"""
    main = tmp_path / "swarm.log"
    short = "beef0001"
    _write(main, [f"[task={short}] 行{i}" for i in range(50)])
    with patch("swarm.logging_config.resolve_log_path", return_value=main):
        lines = read_task_logs("beef0001zzzz", limit=10)
    assert len(lines) == 10, f"应只返回最新 limit=10 行，实得 {len(lines)}"
    assert "行49" in lines[-1] and "行40" in lines[0]


def test_empty_when_no_match(tmp_path):
    main = tmp_path / "swarm.log"
    _write(main, ["[task=zzzz9999] 别的任务"])
    with patch("swarm.logging_config.resolve_log_path", return_value=main):
        assert read_task_logs("abcd1234", limit=500) == []


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
