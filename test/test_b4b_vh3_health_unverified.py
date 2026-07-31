"""B-4b V-H3：3xx/4xx 不再冒充"健康端点应答"（27 号文 §3.3 HIGH）。

原病灶：`classify_smoke_outcome` 只拦 5xx，其余非 000 一律 `passed:started`。而
`_HEALTH_ENDPOINT_MARKERS` 只认 4 条（全 JVM/Nest）→ Django/Flask/FastAPI/Gin/Express/
Rails/Laravel/.NET 一律回退探 `/`，裸 API 对 `/` 返 404 是常态 → 第三道确定性闸对这些栈
实际只证明了"端口通"（与 `started_tcp_only` 同强度），却报满格通过、且无任何 degraded 留痕。

治法方向刻意是 **passed + degraded**（不是 failed）：对 `/` 返 404 完全合法，判失败即误杀。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from swarm.brain.nodes.runtime_smoke import classify_smoke_outcome  # noqa: E402


def _probe(code: str) -> list[str]:
    return [f"ok:{code}"]


@pytest.mark.parametrize("code", ["404", "403", "401", "302", "301"])
def test_non_2xx_answer_is_passed_but_degraded(code):
    """★V-H3 本尊★ 3xx/4xx → 仍 passed（不误杀）但必须 degraded + 独立 classification。

    突变判据：把 `if _http_code[:1] in ("3", "4")` 整块删掉 → 回到 `passed:started`，本条必红。
    """
    # provenance 显式给 True：F-2 后两档分开（回退 `/` 走 started_no_health_contract），
    # 本条锁的是"3xx/4xx 不再冒充满格通过"这件事，故固定在信号更强的那一档上断言。
    res = classify_smoke_outcome("alive", "", _probe(code), health_path_derived=True)
    assert res.status == "passed", f"3xx/4xx 被判 {res.status}＝误杀方向"
    assert res.classification == "started_health_unverified", res.classification
    assert res.details.get("degraded") is True, res.details
    assert res.details.get("probe_health_verified") is False, res.details


def test_2xx_still_reports_full_pass():
    """★对照臂★ 200 照旧 `passed:started` 无 degraded——没有它，"一律 degraded"也能过上面。"""
    res = classify_smoke_outcome("alive", "", _probe("200"))
    assert res.classification == "started", res.classification
    assert not res.details.get("degraded"), res.details


def test_5xx_path_unchanged():
    """对照臂：5xx 仍是 `skipped:http_server_error`（本批不许动它——沙箱无 DB 时 5xx 常为环境）。"""
    res = classify_smoke_outcome("alive", "", _probe("503"))
    assert res.status == "skipped" and res.classification == "http_server_error"


def test_tcp_only_path_unchanged():
    """对照臂：无 curl 的 TCP 兜底仍是 `started_tcp_only`（两条降级语义不互串）。"""
    res = classify_smoke_outcome("alive", "", ["ok"])
    assert res.classification == "started_tcp_only", res.classification


@pytest.mark.parametrize("derived,expect", [(True, True), (False, False)])
def test_probe_target_provenance_is_machine_readable(derived, expect):
    """探的是"证据推出的健康端点"还是"回退 `/`"必须机读可辨。

    404 在两者下的含义不同（前者疑路由没注册，后者多半只是没有根路由）——共用一句话
    会让判读的人查错方向（B-4a CRITICAL-3 同族）。
    """
    res = classify_smoke_outcome("alive", "", _probe("404"), health_path_derived=derived)
    assert res.details.get("probe_target_derived") is expect, res.details
    assert ("健康端点" if derived else "`/`") in res.message, res.message


def test_degraded_pass_reason_reaches_state_and_blocks_l6():
    """★接线证明★ 新 classification 必须真的产出 degraded 账、且该账阻断 L6。

    不新造账是刻意的：verify.py 对 `classification != "started"` 已自动产
    `runtime_smoke_degraded_pass:<cls>`。本条证明这条既有通道真的把新档带出来了
    （"新账没有消费者＝没造"的反向验证）。
    突变判据：把 classification 改回 `"started"` → 账消失，本条必红。
    """
    from swarm.memory.pattern_extractor import blocking_degraded_reasons

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_q_batch_runtime_gate import _run_verify_runtime

    # ★用**生产分类器的真实产出**喂 verify_runtime，绝不手工构造 RuntimeSmokeResult★
    # 第一版这里用 `_smoke_result("passed", "started_health_unverified", …)` 手写档位名，
    # 于是把生产里的 classification 改回 `"started"` 时本条照旧绿（突变 S2 当场证实）——
    # 测的是"verify.py 会把非 started 变成账"，而不是"V-H3 的新档位真的流到了账上"。
    # 这正是 B-4a CRITICAL-1 那一族（手工构造 state → 测不到生产接线）。
    res = classify_smoke_outcome("alive", "", _probe("404"), health_path_derived=True)
    assert res.classification == "started_health_unverified", res.classification
    out = _run_verify_runtime(res)
    reasons = out.get("degraded_reasons") or []
    assert any(r.startswith("runtime_smoke_degraded_pass:started_health_unverified")
               for r in reasons), reasons
    assert blocking_degraded_reasons(reasons), (
        f"降级通过没有阻断 L6 成功学习（会把『没验到健康』学成成功模式）：{reasons}")


# ══════════════════════════════════════════════
# F-2（hunter 复核）：按 provenance 分档——两档消费契约不同
# ══════════════════════════════════════════════

def test_no_health_contract_is_informational_not_l6_blocking():
    """★F-2★「压根没推出健康端点、探的是回退 `/`」→ 信息性档，**不**拦 L6。

    `_HEALTH_ENDPOINT_MARKERS` 只认 4 条（全 JVM/Nest），而"裸 API 对 `/` 返 404"正是
    V-H3 的立项论据＝**常态**。若它也走阻断档，`degraded_reasons` 对所有非 actuator 栈
    恒非空 → `should_write_success` 恒 False → L6 成功学习通道**永久归零且无信号**。
    这就是"复用单一事实源 ≠ 复用其消费契约"：账可共享，后果不同必须分档。
    突变判据：把该档从 `INFORMATIONAL_DEGRADED_PREFIXES` 删掉，本条必红。
    """
    from swarm.memory.pattern_extractor import blocking_degraded_reasons

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_q_batch_runtime_gate import _run_verify_runtime

    res = classify_smoke_outcome("alive", "", _probe("404"), health_path_derived=False)
    assert res.classification == "started_no_health_contract", res.classification
    out = _run_verify_runtime(res)
    reasons = out.get("degraded_reasons") or []
    assert any("started_no_health_contract" in r for r in reasons), (
        f"降级事实必须仍然可见（只是不拦 L6）：{reasons}")
    assert not blocking_degraded_reasons(reasons), (
        f"覆盖缺口档不该拦 L6（会把非 actuator 栈的成功学习永久归零）：{reasons}")


def test_derived_health_endpoint_failing_still_blocks_l6():
    """★F-2 对照臂★ 证据推出的健康端点返非 2xx ＝**真信号**（路由没注册/端点坏了）→ 仍拦 L6。

    没有这条，"把两档都列进信息性白名单"也能满足上面（那会让 V-H3 的阻断力整体归零）。
    """
    from swarm.memory.pattern_extractor import blocking_degraded_reasons

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_q_batch_runtime_gate import _run_verify_runtime

    res = classify_smoke_outcome("alive", "", _probe("404"), health_path_derived=True)
    assert res.classification == "started_health_unverified", res.classification
    reasons = _run_verify_runtime(res).get("degraded_reasons") or []
    assert blocking_degraded_reasons(reasons), (
        f"证据推出的健康端点坏了是真信号，必须拦 L6：{reasons}")
