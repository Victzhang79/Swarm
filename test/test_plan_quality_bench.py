"""CI 回归:plan-quality 离线评测全夹具必须过。

每改 brain 的 planning 确定性 pass(resolve_plan_conflicts 及其子 pass),本测试秒级守护
"真实 E2E 失败 plan 经冲突解决后达成 plan_validator 不变量",免再靠 $30/次 live E2E 撞 bug。
"""

from __future__ import annotations

import pytest

from test.benchmark.plan_quality.plan_quality_bench import run_all


def _params():
    """★已知缺口用 xfail(strict=True)（B-0 红灯先行）★

    夹具里写的永远是【正确期望】，绝不迁就现状——把期望改成"现在这样"才是真假绿。
    非 Maven 夹具当前必红（判死名单 ⊅ 收敛名单等三条，见 manifest 的 known_gap），
    strict 保证：B-3 修好那天它们变 XPASS → 测试**失败** → 逼人回来摘标记，
    不会留下"早就修好了但标记还挂着"的僵尸豁免。
    """
    out = []
    for r in run_all():
        marks = [pytest.mark.xfail(reason=r.known_gap, strict=True)] if r.known_gap else []
        out.append(pytest.param(r, marks=marks, id=f"{r.run}:{r.file}"))
    return out


@pytest.mark.parametrize("result", _params())
def test_plan_quality_fixture(result):
    assert not result.violations, f"{result.run} 违反不变量: {result.violations}"
    assert result.expectations_met, f"{result.run} 期望未达成: {result.notes}"
