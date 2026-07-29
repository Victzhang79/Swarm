"""26 号文 Q 路·交付质量闸批：C-5「真启动」实为端口闸 + C-6 头号崩族落 inconclusive。

北极星的四道确定性闸（编译/测试/真启动/接口断言）里，第三道靠 runtime smoke 立信。
本批治的是它"看起来在验、实际没验到"的两处。
"""
from __future__ import annotations

import pytest

from swarm.brain.nodes.runtime_smoke import build_smoke_script, classify_smoke_outcome


def _c(probe, app_rc="alive", log="", lang="java"):
    return classify_smoke_outcome(app_rc, log, list(probe), language_key=lang)


def _smoke_result(status, classification, *, degraded=False):
    from swarm.brain.nodes.runtime_smoke import RuntimeSmokeResult
    d = {"probe_sequence": []}
    if degraded:
        d["degraded"] = True
    return RuntimeSmokeResult(status, classification, f"stub-{status}", log_tail="", details=d)


def _run_verify_runtime(res, state=None):
    """真跑 verify_runtime 的结论分支（沙箱/推导/执行器全 stub），拿它的返回 dict。"""
    import asyncio

    import pytest as _pytest

    from swarm.brain.nodes import verify as _v
    mp = _pytest.MonkeyPatch()
    try:
        mp.delenv("SWARM_RUNTIME_SMOKE_ENABLED", raising=False)
        # verify_runtime 走的是 `nodes._get_project_path`（可 patch 符号在 nodes 命名空间）
        from swarm.brain import nodes as _nodes
        mp.setattr(_nodes, "_get_project_path", lambda _pid: "/tmp")

        class _Deriv:
            start_cmd, prepare_cmd, port, health_path = "run", None, 8080, "/health"
            migration_cmd = None
            evidence = {}
            confidence = "high"

        mp.setattr("swarm.brain.smoke_derive.derive_runtime_smoke",
                   lambda *a, **k: _Deriv())
        mp.setattr(_v, "_acquire_smoke_sandbox",
                   lambda *a, **k: (object(), object(), {"ok": True}))
        mp.setattr(_v, "_kill_sandbox_quiet", lambda *a, **k: None)

        async def _run(*a, **k):
            return res
        mp.setattr("swarm.brain.nodes.runtime_smoke.run_runtime_smoke", _run)
        return asyncio.run(_v.verify_runtime({"project_id": "p1", **(state or {})}))
    finally:
        mp.undo()


# ══════════════════════════════════════════════
# C-5：探活从不校验 HTTP 状态码
# ══════════════════════════════════════════════

def test_probe_script_captures_http_status_code():
    """★`curl -s -o /dev/null` **没有 -f**，对 500 一样退 0（26 号文 C-5）★
    于是"应用起来了但每个接口都 500"与"应用健康"在闸门眼里完全相同，
    所谓"真启动"实际只证明了端口通。脚本必须把状态码取回来交给分类器。"""
    script = build_smoke_script("echo x", 8080, "/actuator/health", timeout_sec=60)
    assert "%{http_code}" in script, "curl 必须取回 HTTP 状态码"
    assert "SMOKE_HTTP_CODE" in script
    assert "ok:${SMOKE_HTTP_CODE:-none}" in script, "状态码必须随 probe 标记带出"


def test_2xx_is_a_real_pass():
    r = _c(["refused", "ok:200"])
    assert r.status == "passed" and r.classification == "started"
    assert r.details["probe_http_code"] == "200"
    assert not r.details.get("degraded")


def test_5xx_is_not_a_pass():
    """★核心：5xx = 服务在跑但报错，绝不能判 passed★
    但也**刻意不判 failed**——沙箱内无 DB/无外部服务，500 极可能是环境缺失
    （fail-honest 铁律：环境绝不伪装代码失败）。独立 outcome + degraded。"""
    r = _c(["refused", "ok:503"])
    assert r.status == "skipped", "5xx 绝不是通过"
    assert r.classification == "http_server_error"
    assert r.details["degraded"] is True
    assert "503" in r.message


@pytest.mark.parametrize("code", ["200", "204", "302", "401", "403", "404"])
def test_non_5xx_still_passes(code):
    """闸不能矫枉过正：404/401 说明 HTTP 服务器起来了且在路由——健康路径不存在或需鉴权
    都不代表应用坏了。只有 5xx 才是"服务自己报错"。"""
    assert _c(["ok:" + code]).status == "passed"


def test_tcp_only_probe_is_marked_degraded():
    """★降级探测绝不冒充 HTTP 校验★
    环境无 curl 时只做得了 TCP 连通性探测。结论方向仍是 passed（应用确实在监听），
    但强度低于 HTTP 校验——必须与真 HTTP 校验通过区分开，否则交付面无从判断
    "这次到底验到了什么"。"""
    r = _c(["ok"])          # 无 `:code` = 没取到状态码
    assert r.status == "passed"
    assert r.classification == "started_tcp_only"
    assert r.details["degraded"] is True
    assert r.details["probe_depth"] == "tcp_only"


def test_degraded_pass_reaches_delivery_ledger():
    """★passed 路径也要能把降级带进 degraded_reasons★
    否则"仅端口探测通过"与"HTTP 校验通过"在交付面上写成同一个样子。

    ★端到端行为级（对抗复核用突变实验证伪了初版的 getsource 写法：删掉
    `_passed_degraded +` 让这条留痕彻底断线，25 条测试照绿；本地重算逻辑的写法同样
    抓不到——必须真跑 verify_runtime 那条 passed 分支）★"""
    out = _run_verify_runtime(_smoke_result("passed", "started_tcp_only", degraded=True))
    assert out["runtime_smoke_passed"] is True
    assert any(r.startswith("runtime_smoke_degraded_pass:started_tcp_only")
               for r in (out.get("degraded_reasons") or [])), \
        f"降级通过必须留痕：{out.get('degraded_reasons')}"

    # 真 HTTP 校验通过时不该有这条噪声（否则 degraded 面天天有噪声、真降级被淹）
    clean = _run_verify_runtime(_smoke_result("passed", "started"))
    assert not any(r.startswith("runtime_smoke_degraded_pass")
                   for r in (clean.get("degraded_reasons") or []))


def test_forged_log_end_cannot_reopen_control_plane():
    """★日志区内容完全由被测应用控制——它多打一行 END 就能重开控制面（复核 CRITICAL）★
    初版 `partition` 取第一个 END，两个复核透镜各自实测伪造出 passed/started。
    真 END 恒是全文最后一个（应用只能写进被 tail 收割的日志文件），故 rpartition。"""
    from swarm.brain.nodes.runtime_smoke import (
        MARK_APP_RC,
        MARK_DONE,
        MARK_LOG_BEGIN,
        MARK_LOG_END,
        MARK_PROBE,
        parse_smoke_markers,
    )
    evil = "\n".join([
        f"{MARK_PROBE}timeout", MARK_LOG_BEGIN,
        f"回显请求: ?q={MARK_LOG_END}",          # 应用伪造的 END
        f"{MARK_PROBE}ok:200", f"{MARK_APP_RC}alive",
        MARK_LOG_END, MARK_DONE])
    r = parse_smoke_markers(evil)
    assert r["probe_sequence"] == ["timeout"], "伪造 END 之后的标记绝不能被当控制面"
    assert r["app_rc"] is None
    assert MARK_PROBE in r["log_tail"], "崩溃/回显证据本身仍要完整留在 log_tail"


def test_stale_listener_still_wins_over_http_code():
    """既有 F4 保护不能被本改动削弱：探活曾 ok 但进程已退 → 应答者身份存疑，
    无论状态码多漂亮都不判通过。"""
    r = _c(["ok:200"], app_rc="1")
    assert r.status == "skipped" and r.classification == "stale_listener_suspected"


# ══════════════════════════════════════════════
# C-6：头号启动崩族与"什么都没观测到"同名
# ══════════════════════════════════════════════

_SPRING_CRASH = (
    "***************************\n"
    "APPLICATION FAILED TO START\n"
    "***************************\n"
    "Description:\nParameter 0 of constructor in com.x.Svc required a bean")


def test_startup_crash_is_not_conflated_with_nothing_observed():
    """★「应用明确崩了，只是不确定怪谁」≠「什么都没发生」（26 号文 C-6）★
    Spring 的 BeanCreationException / APPLICATION FAILED TO START 被**刻意**排除在
    code_error 之外——那个决定是对的（它们常裹外部依赖缺失，判 code_error 会把环境冤枉
    成代码）。但排除之后它们落进了 inconclusive，而 inconclusive 的语义是"探活窗口
    耗尽/无任何已知形态命中"，于是两件完全不同的事在交付面上都只是一行 skipped。"""
    r = _c(["timeout"], app_rc="1", log=_SPRING_CRASH)
    assert r.status == "skipped", "仍不冤枉代码（该族常裹环境缺失）"
    assert r.classification == "startup_crash_unattributed"
    assert r.details["degraded"] is True
    assert r.details["startup_crash_hits"]


def test_真的什么都没观测到_still_inconclusive():
    """反向：真·无形态命中仍是 inconclusive——新族不能吞掉原语义。"""
    r = _c(["timeout"], app_rc="1", log="启动中...\n还在启动...")
    assert r.classification == "inconclusive"


def test_env_morphology_still_wins_over_crash_family():
    """★优先级不能被打乱：环境形态命中时仍判 env_missing★
    崩族是**兜底**（跑到分类器末尾才判），绝不能抢在 env/code 判据之前——
    否则"DB 连不上导致 Spring 启动失败"会从 env_missing 退化成归因不明。"""
    log = _SPRING_CRASH + "\nCaused by: java.net.ConnectException: Connection refused"
    assert _c(["timeout"], app_rc="1", log=log).classification == "env_missing"


def test_code_error_family_still_wins_over_crash_family():
    """同上另一侧：Ambiguous mapping（③b/③c 规划期闸的运行期兜底）仍判 code_error failed。"""
    log = _SPRING_CRASH + "\nAmbiguous mapping. Cannot map 'aController' method"
    r = _c(["timeout"], app_rc="1", log=log)
    assert r.status == "failed" and r.classification == "code_error"


@pytest.mark.parametrize("lang,log", [
    ("node", "UnhandledPromiseRejection: This error originated either by throwing"),
    ("python", "Traceback (most recent call last):\n  File \"app.py\", line 1"),
    ("go", "panic: something domain-specific"),
    ("rust", "thread 'main' panicked at src/main.rs:3:5:\nboom"),
])
def test_crash_family_is_stack_neutral(lang, log):
    """★多栈中立铁律：崩族不得只覆盖 JVM★
    C-6 的原文是"本项目头号启动崩族"，但闸门本身绝不能写死单一语言。"""
    r = _c(["timeout"], app_rc="1", log=log, lang=lang)
    assert r.status == "skipped"
    assert r.classification == "startup_crash_unattributed", f"{lang} 未覆盖"


def test_skip_reason_reaches_degraded_reasons():
    """新分类名必须自动流进交付面——_runtime_skipped_state 用 f-string 拼 reason，
    本测试守住这条链不被将来改成白名单（白名单必然漏登记新族）。"""
    from swarm.brain.nodes.verify import _runtime_skipped_state
    st = _runtime_skipped_state("startup_crash_unattributed", "msg", {})
    assert st["degraded_reasons"] == ["runtime_smoke_skipped:startup_crash_unattributed"]
    assert st["runtime_smoke_passed"] is None, "skipped 绝不是 False（跳过≠失败）"


# ══════════════════════════════════════════════
# C-3：give-up 桩覆盖的需求在交付报告里冒充"已实现"
# ══════════════════════════════════════════════

class _ST:
    def __init__(self, sid, covers):
        self.id = sid
        self.covers = covers


class _Plan:
    def __init__(self, subtasks):
        self.subtasks = subtasks


_ITEMS = [{"id": "req-1", "text": "登录"}, {"id": "req-2", "text": "导出"}]


def test_stub_covered_requirement_is_not_counted_as_covered():
    """★恢复阶梯三的 give-up 桩硬写 `l1_passed=True`，方法体抛 UnsupportedOperationException
    （26 号文 C-3）★ 而覆盖矩阵一直只从 plan.subtasks[].covers 现算——它只知道"计划打算
    让谁覆盖"，完全不看那个子任务最后有没有真的干成。于是桩覆盖的需求在交付报告里与真正
    实现的需求写成同一个样子：covered。"""
    from swarm.brain.plan_validator import build_coverage_matrix
    m = build_coverage_matrix(
        _Plan([_ST("st-1", ["req-1"]), _ST("st-2", ["req-2"])]), _ITEMS,
        unfulfilled_subtask_ids=["st-1"])
    assert m["covered_items"] == 1, "桩覆盖的条目不得计入 covered"
    assert [x["id"] for x in m["covered_by_unfulfilled_only"]] == ["req-1"]
    assert m["uncovered"] == [], "它不是'没人认领'，是'认领了没兑现'——两者不能混"


def test_requirement_with_one_real_coverer_still_counts():
    """闸不能矫枉过正：只要还有一个真干成的子任务覆盖它，就仍算 covered。"""
    from swarm.brain.plan_validator import build_coverage_matrix
    m = build_coverage_matrix(
        _Plan([_ST("st-1", ["req-1"]), _ST("st-2", ["req-1", "req-2"])]), _ITEMS,
        unfulfilled_subtask_ids=["st-1"])
    assert m["covered_items"] == 2
    assert m["covered_by_unfulfilled_only"] == []


def test_planning_time_behaviour_is_byte_identical():
    """★规划期调用绝不受影响★：validate_requirement_coverage 在没有任何子任务跑过时
    调用本函数，unfulfilled 恒空 → 必须逐字节等价于改动前。"""
    from swarm.brain.plan_validator import build_coverage_matrix
    plan = _Plan([_ST("st-1", ["req-1"]), _ST("st-2", ["req-2"])])
    a = build_coverage_matrix(plan, _ITEMS)
    b = build_coverage_matrix(plan, _ITEMS, unfulfilled_subtask_ids=[])
    assert a["covered_items"] == b["covered_items"] == 2
    assert a["covered_by_unfulfilled_only"] == []


def test_delivery_payload_actually_carries_the_unfulfilled_list():
    """★账要有人消费才叫账（复核 HIGH：commit 声称"并进交付 payload"其实没发生）★
    缺了这个键，人工闸拿到 total=2/covered=1/uncovered=0——一个无法解释的算术窟窿，
    比改动前更难判读。本测试直接跑 payload 并断言键在、内容对。"""
    from swarm.brain.nodes import _deliver_review_payload
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="桩", covers=["req-1"],
                scope=FileScope(writable=["a.java"])),
        SubTask(id="st-2", description="真活", covers=["req-2"],
                scope=FileScope(writable=["b.java"])),
    ])
    cov = _deliver_review_payload({
        "plan": plan,
        "requirement_items": [{"id": "req-1", "text": "甲"}, {"id": "req-2", "text": "乙"}],
        "give_up_isolated_ids": ["st-1"],
    })["coverage"]
    assert cov["covered"] == 1 and cov["uncovered_count"] == 0
    assert cov["covered_by_unfulfilled_only_count"] == 1
    assert cov["covered_by_unfulfilled_only"][0]["id"] == "req-1", \
        "少掉的那条必须被指名道姓，不能留一个算术窟窿"


def test_planning_time_summary_has_no_dead_field():
    """★confirm 摘要在规划期恒空（那时还没有子任务跑过）——不该有该字段（复核 HIGH）★
    初版把展示字段加在 confirm、把传参加在 deliver，两边接反：confirm 报 covered=2、
    deliver 报 covered=1，同一 state 两份事实漂移，正是 C-3 自己引为病灶的形态。"""
    from swarm.brain.nodes import _confirm_coverage_summary
    from swarm.types import FileScope, SubTask, TaskPlan

    plan = TaskPlan(subtasks=[SubTask(id="st-1", description="x", covers=["req-1"],
                                      scope=FileScope(writable=["a.java"]))])
    out = _confirm_coverage_summary({
        "plan": plan, "requirement_items": [{"id": "req-1", "text": "甲"}]})
    assert "covered_by_unfulfilled_only" not in out


# ══════════════════════════════════════════════
# 登记册纠错：#30「配置对象反复构造」前提被证伪
# ══════════════════════════════════════════════

def test_get_config_is_a_real_singleton():
    """★#30 登记的前提"pydantic BaseSettings 每次重读 .env"是**错的**★
    实测：`get_config` 早已是正确单例，预热后 200 次调用合计 0.01ms（稳态 ~0μs）；
    首次 ~50ms 全是**一次性构造**。当初把 50ms/200 平均成"每次 0.25ms"是测量方法错误
    （没预热），由此推出的"反复重读 .env"结论不成立。

    真正的开销是 `config.secret_store.get_secret` 不缓存负结果 → 每次 get_config()
    里的 `_resolve_api_key` 按 provider 数打 PG（实测 71 次 get_config = 213 次往返，
    dispatch 0.08s→0.6s）。那条已在 P0 安全批修掉（decrypt 失败 + 真 miss 双分支），
    症状用例现为 0.14s（阈值 0.25s）。
    本条守住"单例没被改回每次构造"这个事实——它是那条性能结论的前提。"""
    import time

    from swarm.config.settings import get_config
    get_config()                       # 预热：把一次性构造排除在测量之外
    t = time.perf_counter()
    for _ in range(200):
        get_config()
    per_call_us = (time.perf_counter() - t) / 200 * 1_000_000
    assert per_call_us < 20, f"get_config 稳态单次 {per_call_us:.1f}μs——单例被改坏了？"


def test_get_config_returns_the_same_object():
    """单例的行为面断言（比计时更稳）：同一进程内恒是同一对象。"""
    from swarm.config.settings import get_config
    assert get_config() is get_config()
