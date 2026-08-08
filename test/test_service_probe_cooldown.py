"""#29-4 T-7 复核 M-5 整改锁：服务探测失败**不得永久锁存**，须带冷却重探。

## 病理（我在测试侧重犯了生产侧已修的病）

第一版 `_SERVICE_PROBE_CACHE` 一次失败就缓存整个 session。而
`infra/redis_client.py:17-24` 的注释里写着同一件事的复盘：

> 旧实现用布尔 `_redis_checked` 永久锁存失败 → 启动期一次瞬时抖动会让整个进程
> 永久退化为内存锁(永不重连) → 多副本 split-brain 风险。改为带冷却的重探。

也就是说这个坑生产侧踩过、修过、还把教训写在代码里了 —— 我在 30 行之外的新代码里
原样复现了一遍。后果：长 session 里 PG 抖 1 秒，之后**所有** PG 用例全 skip
（CI 档则全部硬失败），一次抖动定生死。

## 本文件锁什么

① 失败后**冷却窗内**不重连（控开销，且吸收瞬时抖动）；
② 冷却**到期后**会重探，服务恢复了就能重新真跑；
③ 成功结论永久缓存（不每次都连）。
"""
from __future__ import annotations

import pytest


@pytest.fixture
def _clean_probe_state(service_probe_internals):
    """每条测试前后清掉探测状态，避免污染真实用例的服务判定。

    ★同时必须还原 `_SERVICE_ABSENT_SEEN` / `_SERVICE_FAILED_HARD`★：
    本文件用假探针**故意**造失败，会把 "pg" 记进那两本会话级账 ⇒ 会话末汇总打出一条
    `SERVICE_ABSENT(skip): pg`，而本轮 PG 其实好着。那是**假信号**——恰恰是"恒发的告警
    等于没有告警"那一族（人会学会忽略它，真缺席那天也就看不见了）。
    """
    st = service_probe_internals
    snap = (dict(st["cache"]), dict(st["failed_at"]),
            set(st["absent_seen"]), set(st["failed_hard"]))
    st["cache"].clear()
    st["failed_at"].clear()
    try:
        yield st
    finally:
        st["cache"].clear()
        st["cache"].update(snap[0])
        st["failed_at"].clear()
        st["failed_at"].update(snap[1])
        st["absent_seen"].clear()
        st["absent_seen"].update(snap[2])
        st["failed_hard"].clear()
        st["failed_hard"].update(snap[3])


def test_failure_is_not_locked_in_forever(_clean_probe_state, monkeypatch):
    """★核心★ 失败后冷却到期必须**重探**，服务恢复即可重新真跑。

    突变判据：把冷却逻辑改回"永久锁存"，本条会红（服务恢复后仍 skip）。
    """
    st = _clean_probe_state
    calls = {"n": 0}
    state = {"err": "boom: 模拟抖动"}

    def _fake_probe():
        calls["n"] += 1
        return state["err"]

    monkeypatch.setitem(st["probes"], "pg", _fake_probe)

    # 第一次：失败 → skip
    with pytest.raises(BaseException):     # pytest.skip 抛的是 Skipped
        st["require"]("pg")
    assert calls["n"] == 1

    # 冷却窗内再问：不得重连（沿用上次结论）
    with pytest.raises(BaseException):
        st["require"]("pg")
    assert calls["n"] == 1, (
        f"冷却窗内又去连了一次（探测 {calls['n']} 次）⇒ 每个用例都要付一次连接超时的代价")

    # 把上次失败时刻推到冷却之外 → 必须重探
    st["failed_at"]["pg"] = st["failed_at"]["pg"] - st["cooldown"] - 1
    state["err"] = None                      # 服务恢复了

    # ★这里绝不能直接 `st["require"]("pg")`★（我第一版就是，突变实验当场证伪）：
    # 冷却逻辑被改坏时 require 会抛 `Skipped`，而 pytest 把它当成**本条测试被 skip**
    # ⇒ 报 SKIPPED 而非 FAILED、rc=0 ⇒ 突变"仍绿"。
    # 那正是本批一直在治的形态（静默 skip 冒充通过）——我在验它的测试里又写了一遍。
    # 必须把 Skipped 接住并显式判失败。
    try:
        st["require"]("pg")
    except BaseException as exc:             # noqa: BLE001  Skipped 继承 BaseException
        pytest.fail(
            f"冷却到期且服务已恢复，require 仍然让用例退场（{type(exc).__name__}）⇒ "
            f"失败结论被永久锁存：一次瞬时抖动让整轮所有该服务的用例降级。"
            f"生产侧 infra/redis_client.py:17-24 为这个病加过冷却重探，测试侧不该重犯。")
    assert calls["n"] == 2, (
        f"冷却到期后没有重探（探测次数仍为 {calls['n']}）⇒ 失败结论被永久锁存")


def test_success_is_cached_permanently(_clean_probe_state, monkeypatch):
    """成功结论只探一次 —— 否则 7000+ 用例每条都连一次库。

    这条与上一条是一对：只有上一条时，把缓存整个删掉也能绿（每次都重探）。
    """
    st = _clean_probe_state
    calls = {"n": 0}

    def _fake_probe():
        calls["n"] += 1
        return None

    monkeypatch.setitem(st["probes"], "pg", _fake_probe)
    for _ in range(5):
        st["require"]("pg")
    assert calls["n"] == 1, f"可用结论被重复探测了 {calls['n']} 次"


def test_unknown_service_name_is_fail_closed(_clean_probe_state):
    """服务名拼错必须抛，不许静默放行（与 M-4 漏名 fail-closed 同向）。"""
    st = _clean_probe_state
    with pytest.raises(ValueError, match="未知服务"):
        st["require"]("pgg")
