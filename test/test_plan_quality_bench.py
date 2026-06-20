"""CI 回归:plan-quality 离线评测全夹具必须过。

每改 brain 的 planning 确定性 pass(resolve_plan_conflicts 及其子 pass),本测试秒级守护
"真实 E2E 失败 plan 经冲突解决后达成 plan_validator 不变量",免再靠 $30/次 live E2E 撞 bug。
"""

from __future__ import annotations

import pytest

from test.benchmark.plan_quality.plan_quality_bench import run_all


@pytest.mark.parametrize("result", run_all(), ids=lambda r: f"{r.run}:{r.file}")
def test_plan_quality_fixture(result):
    assert not result.violations, f"{result.run} 违反不变量: {result.violations}"
    assert result.expectations_met, f"{result.run} 期望未达成: {result.notes}"
