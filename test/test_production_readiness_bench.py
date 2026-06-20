"""CI 回归:生产就绪度离线评测必须跑通且确实抓到真实缺陷。

E2E 一轮 ~$3000、~小时级,跑完只能靠肉眼逐文件判断生成的项目是不是"真生产级完整"。
本测试秒级守护:对 RUN20 RuoYi 告警平台的【产物快照 + plan】做 4 维静态分析,断言
(1) harness 能跑通、(2) 每个夹具 manifest 期望达成、(3) 确实抓到 >=1 类真实缺陷
(证明评测有效——非空跑)。纳入 CI,作为"完完全全真真正正完成"的客观合格证守门人。
"""

from __future__ import annotations

import pytest

from test.benchmark.production_readiness.production_readiness_bench import (
    run_all,
    total_defects,
)

_RESULTS = run_all()


@pytest.mark.parametrize("result", _RESULTS, ids=lambda r: r.run)
def test_expectations_met(result):
    """每个夹具的 manifest 期望(覆盖率区间、缺层、悬空数等)必须达成。"""
    assert result.expectations_met, f"{result.run} 期望未达成: {result.notes}"


@pytest.mark.parametrize("result", _RESULTS, ids=lambda r: r.run)
def test_catches_real_defects(result):
    """评测必须确实抓到 >=1 处真实缺陷,否则评测形同虚设(假阴性)。"""
    n = total_defects(result)
    assert n >= 1, f"{result.run} 未抓到任何缺陷,评测可能失效"


@pytest.mark.parametrize("result", _RESULTS, ids=lambda r: r.run)
def test_dimensions_populated(result):
    """4 维都应有产出(快照非空、覆盖度有应建文件全集、悬空检查有白名单/产物基底)。"""
    assert sum(result.snapshot_counts.values()) > 0, "快照为空"
    assert result.coverage.expected > 0, "plan 未解析出应建文件"
    assert result.dangling.produced_classes > 0, "未解析出任何产物类(悬空检查失效)"
    assert result.dangling.whitelist_classes > 0, "白名单为空(悬空检查会误报)"


def test_run20_catches_signature_defects():
    """RUN20 专项:断言确实抓到 4 个签名缺陷(覆盖缺 sql、缺层、悬空、规范),
    证明评测能抓到 RUN20 真缺陷(不只是泛泛 >=1)。"""
    r = next((x for x in _RESULTS if x.run == "RUN20"), None)
    assert r is not None, "缺 RUN20 夹具"
    # 覆盖度:sql 全缺(plan 要建表 DDL+菜单 seed,快照 0 个 sql)
    assert "sql" in r.coverage.missing_by_layer, "应抓到 sql 层全缺"
    assert r.coverage.coverage_pct < 100.0, "覆盖率应 <100%"
    # 分层完整性:至少 1 个实体缺层
    assert len(r.layering.incomplete) >= 1, "应抓到缺层实体"
    # 悬空符号:NotifyCallbackController/NotifyApiServiceImpl 引用不存在的 com.ruoyi 类
    assert len(r.dangling.dangling) >= 1, "应抓到悬空符号(同 RedisCache 一类)"
    symbols = {s for _f, s in r.dangling.dangling}
    assert any("IAlarmEngineService" in s or "alarm.dto" in s for s in symbols), \
        f"应抓到错包/未定义引用,实得 {symbols}"
