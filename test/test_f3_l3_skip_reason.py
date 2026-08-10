"""LOW 收口 F3：`l3_skip_reason` 机读键全链 + 分支 3/4/6 degraded 挡 L6 毒化。

治前 verify_l3 的 6 处跳过 return 逐字同构（l3_passed=None + l3_skipped=True），机读
不可辨是【哪种】跳过；其中 push_path_unavailable / push_failed / llm_unavailable 三分支
（观测基建没能观测）连 degraded 都不写 ⇒ L6 should_write_success 把「L3 没验」学成
成功模式（毒化断链，比登记缺失更重）。治法三件套（对齐 _runtime_skipped_state 先例）：
  ① 6 跳过分支 always-emit 具体 reason；通过/失败路径 always-emit ""（round 生命周期）；
  ② 薄包装格值分档 `skipped:<reason>`（B7-k 先例）+ always-emit 兜底（未来新增出口
     忘带 reason 也不粘滞——本文件的 test_wrapper_always_emit_* 就是这条的锁）；
  ③ 分支 3/4/6 补 degraded_reasons；分支 1/2/5（常态跳过）刻意不写——写了会掐死
     小任务 L6 学习（分档判据=观测基建没观测到≠产物缺席，auto_accept 仍放行不硬拒）。
判据：把任一分支的 reason/degraded 删掉，本文件必有测试红。
"""
from __future__ import annotations

import swarm.brain.l3_gitlab as l3g
import swarm.brain.nodes as N
import swarm.brain.nodes.verify as V
import swarm.brain.runner as runner
from swarm.types import Complexity


def _st(**kw):
    base = {"complexity": Complexity.COMPLEX, "merged_diff": "diff --git a/x b/x",
            "task_id": "t1", "project_id": "p1", "task_description": "d"}
    base.update(kw)
    return base


def _no_gitlab(monkeypatch):
    monkeypatch.setattr(l3g, "gitlab_configured", lambda: False)


# ── 六分支 reason + 格值分档 + degraded 分档（全走生产 verify_l3 薄包装）──

async def test_branch1_complexity_skip_no_degraded():
    out = await V.verify_l3(_st(complexity=Complexity.SIMPLE))
    assert out["l3_skip_reason"] == "complexity_skip"
    assert out["verification_coverage"]["l3"] == "skipped:complexity_skip"
    assert "degraded_reasons" not in out, "常态跳过写 degraded 会掐死小任务 L6 学习"


async def test_branch2_no_merged_diff_no_degraded():
    out = await V.verify_l3(_st(merged_diff=""))
    assert out["l3_skip_reason"] == "no_merged_diff"
    assert out["verification_coverage"]["l3"] == "skipped:no_merged_diff"
    assert "degraded_reasons" not in out


async def test_branch3_push_path_unavailable_has_degraded(monkeypatch):
    monkeypatch.setattr(l3g, "gitlab_configured", lambda: True)
    monkeypatch.setattr(l3g, "l3_push_enabled", lambda: True)
    monkeypatch.setattr(N, "_get_project_path", lambda pid: "")
    out = await V.verify_l3(_st())
    assert out["l3_skip_reason"] == "push_path_unavailable"
    assert out["verification_coverage"]["l3"] == "skipped:push_path_unavailable"
    assert out["degraded_reasons"] == ["l3_skipped:push_path_unavailable"], \
        "观测基建没观测到却不留痕 ⇒ L6 把「L3 没验」学成成功（毒化断链）"


async def test_branch4_push_failed_has_degraded(monkeypatch):
    monkeypatch.setattr(l3g, "gitlab_configured", lambda: True)
    monkeypatch.setattr(l3g, "l3_push_enabled", lambda: True)
    monkeypatch.setattr(N, "_get_project_path", lambda pid: "/proj")
    monkeypatch.setattr(l3g, "push_merged_diff_branch",
                        lambda *a, **k: (None, "boom"))
    out = await V.verify_l3(_st())
    assert out["l3_skip_reason"] == "push_failed"
    assert out["verification_coverage"]["l3"] == "skipped:push_failed"
    assert out["degraded_reasons"] == ["l3_skipped:push_failed"]


async def test_branch5_no_staging_url_no_degraded(monkeypatch):
    _no_gitlab(monkeypatch)
    monkeypatch.delenv("SWARM_STAGING_URL", raising=False)
    out = await V.verify_l3(_st())
    assert out["l3_skip_reason"] == "no_staging_url"
    assert out["verification_coverage"]["l3"] == "skipped:no_staging_url"
    assert "degraded_reasons" not in out, "环境未配 staging 是常态，不该 degraded"


async def test_branch6_llm_unavailable_has_degraded(monkeypatch):
    _no_gitlab(monkeypatch)
    monkeypatch.setenv("SWARM_STAGING_URL", "http://staging.example")
    def _boom():
        raise RuntimeError("llm down")
    monkeypatch.setattr(N, "_get_brain_llm", _boom)
    monkeypatch.setattr(V, "_l3_staging_http_check", lambda url: (False, "probe-msg"))
    out = await V.verify_l3(_st())
    assert out["l3_skip_reason"] == "llm_unavailable"
    assert out["verification_coverage"]["l3"] == "skipped:llm_unavailable"
    assert out["degraded_reasons"] == ["l3_skipped:llm_unavailable"], \
        "本批最重一条：LLM 半死 → L3 永远诚实跳过 → 无 degraded 则 L6 学成成功"


# ── 通过/失败路径：reason always-emit "" ──

async def test_gitlab_pass_path_reason_empty(monkeypatch):
    monkeypatch.setattr(l3g, "gitlab_configured", lambda: True)
    monkeypatch.setattr(l3g, "l3_push_enabled", lambda: False)
    monkeypatch.setattr(l3g, "trigger_and_poll_pipeline",
                        lambda **k: (True, "pipeline green"))
    out = await V.verify_l3(_st())
    assert out["l3_passed"] is True
    assert out["l3_skip_reason"] == "", "通过≠跳过，必须显式空串（always-emit 一环）"
    assert out["verification_coverage"]["l3"] == "passed"
    assert "degraded_reasons" not in out


async def test_gitlab_fail_path_reason_empty(monkeypatch):
    monkeypatch.setattr(l3g, "gitlab_configured", lambda: True)
    monkeypatch.setattr(l3g, "l3_push_enabled", lambda: False)
    monkeypatch.setattr(l3g, "trigger_and_poll_pipeline",
                        lambda **k: (False, "pipeline red"))
    out = await V.verify_l3(_st())
    assert out["l3_passed"] is False
    assert out["l3_skip_reason"] == "", "失败≠跳过（_l3_failure_state 同样 always-emit）"
    assert out["verification_coverage"]["l3"] == "failed"


# ── 薄包装 always-emit 兜底：实现体未来新增出口忘带 reason → 不粘滞、格可辨 ──

async def test_wrapper_always_emit_backfill_when_impl_omits_reason(monkeypatch):
    """删掉包装的兜底这两行（l3_skip_reason/skipped 格），本条即红——round 生命周期锁。"""
    async def _impl(state):
        return {"l3_passed": None, "l3_skipped": True, "l3_message": "m"}
    monkeypatch.setattr(V, "_verify_l3_impl", _impl)
    out = await V.verify_l3(_st())
    assert out["l3_skip_reason"] == "", "实现体忘带时包装必须兜底覆写，否则上轮残留粘滞"
    assert out["verification_coverage"]["l3"] == "skipped", \
        "无 reason 的跳过格=裸 'skipped'（与分档格机读可辨）"


# ── deliver 人工闸 payload：l3 块进审核视野 ──

def test_deliver_payload_carries_l3_block():
    payload = N._deliver_review_payload(_st(
        l3_passed=None, l3_skipped=True,
        l3_skip_reason="llm_unavailable", l3_message="L3 LLM validation unavailable"))
    assert payload["l3"] == {
        "passed": None, "skipped": True,
        "reason": "llm_unavailable",
        "message": "L3 LLM validation unavailable",
    }, f"人工闸看不到「L3 是哪种跳过」: {payload['l3']}"


def test_deliver_payload_l3_block_legacy_checkpoint_tolerant():
    """旧 checkpoint 无 l3_skip_reason 键 → reason 空串、绝不抛（interrupt 锚点纪律）。"""
    payload = N._deliver_review_payload({})
    assert payload["l3"]["reason"] == ""
    assert payload["l3"]["passed"] is None


# ── runner 落 DB：l3_skip_reason 随 l3_fields 进 updates ──

def test_runner_sync_carries_l3_skip_reason_to_db(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(runner.store, "update_task",
                        lambda tid, **kw: captured.update(kw))
    runner._sync_task_from_state("t1", {
        "l3_passed": None, "l3_skipped": True,
        "l3_message": "m", "l3_skip_reason": "push_failed",
    })
    assert captured["l3_result"]["l3_skip_reason"] == "push_failed", \
        f"跳过原因没落 DB（l3_fields 元组漏键）: {captured.get('l3_result')}"
