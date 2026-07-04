"""B9（round22, P2）：should_write_success 在 complexity 非法时返回 True，绕过复杂度门槛。

根因：非法 complexity 字符串（typo/污染 state）→ except ValueError: return True → 跳过
SUCCESS_WRITE_MIN_COMPLEXITY(MEDIUM+) 限制，TRIVIAL/未知任务也写 L6 污染知识库。

治本：fail-closed —— 非法值 return False + warning（宁漏学不毒化）。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.memory import pattern_extractor


def _run(complexity):
    with patch("swarm.brain.gates.is_partial_delivery", return_value=False), \
         patch("swarm.brain.gates.can_auto_accept_delivery", return_value=(True, "")):
        return pattern_extractor.should_write_success({"complexity": complexity})


def test_illegal_complexity_fail_closed():
    assert _run("SUPER_BOGUS") is False, "非法 complexity 必须 fail-closed 不写 L6"


def test_trivial_not_written():
    assert _run("trivial") is False, "TRIVIAL 不达 MEDIUM+ 门槛，不写 L6"


def test_medium_written():
    assert _run("medium") is True, "MEDIUM 达门槛，可写 L6"


def test_complex_written():
    assert _run("complex") is True


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
