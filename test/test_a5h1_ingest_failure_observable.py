"""32 号文 A5-H1（唯一 HIGH）治本锁：摄取失败不再静默蒸发需求。

病根：`ingest_errors` 生产侧零消费者 + 摄取层零 `degraded_reasons` ⇒ PRD 部分解析/视觉
理解失败时需求静默缩水，deliver 对着缩水需求集全绿，auto_accept 放行。

四条接线各有锁（判据都是"把机制整块删掉这条会不会红"）：
  ① 写账：errors 非空 → degraded_reasons 带 ingest_partial_failure 前缀
  ② 分档：vision_pending（待人工确认，非失败）绝不触发该前缀
  ③ 挡 auto_accept：can_auto_accept_delivery 显式一臂（本函数不通吃 degraded_reasons，
     只认四个前缀——只写 degraded 会原样穿过去，故这条是真正的承重锁）
  ④ 挡 L6 + 进人工闸 payload
"""
from __future__ import annotations

import asyncio

import pytest

from swarm.brain.gates import can_auto_accept_delivery
from swarm.brain.ingest_node import _ingest_degraded
from swarm.memory.pattern_extractor import blocking_degraded_reasons


# ── ① 写账（纯函数层，前缀格式与计数） ──

def test_errors_produce_degraded_reason():
    out = _ingest_degraded(["a.pdf: 解析失败: bad xref"], ["a.pdf", "b.png"])
    assert out and out[0].startswith("ingest_partial_failure:"), out
    assert out[0] == "ingest_partial_failure:1/2", "前缀后须带 失败数/总数 便于人读"


def test_no_errors_writes_nothing():
    """真的没失败 → 不写键（绝不制造空态粘滞：degraded_reasons 是 append reducer，
    写了就永久留痕，空态写入会让"曾经失败过"与"从没失败"不可分）。"""
    assert _ingest_degraded([], ["a.pdf"]) == []
    assert _ingest_degraded(None, ["a.pdf"]) == []
    assert _ingest_degraded(["", "   "], ["a.pdf"]) == [], "空白串不算失败"


# ── ② 分档：待确认视觉 ≠ 失败 ──

@pytest.mark.asyncio
async def test_vision_pending_alone_is_not_a_failure(monkeypatch, tmp_path):
    """★分档锁★ 图片 PRD 走视觉理解**成功**但待人工确认（ingest_vision_pending 非空、
    errors 空）⇒ 这是正常流程，绝不能触发 ingest_partial_failure。

    混档的后果：每个含图片的 PRD 都会被判"需求可能蒸发"→ 拒 auto_accept ⇒ 闸过宽，
    使用者会绕开它（本仓已有先例：密钥表 HIGH 档冤杀）。
    """
    from swarm.brain import ingest_node

    img = tmp_path / "ui.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    class _Doc:
        filename = "ui.png"
        needs_vision = True
        error = None
        kind = "image"

    class _Res:
        draft_text = ""
        errors: list = []
        documents = [_Doc()]

    class _VRes:
        ok = True
        filename = "ui.png"
        understanding = "登录页"
        model_used = "m"
        confirmed = False
        error = None

    monkeypatch.setattr("swarm.brain.ingest.ingest_files",
                        lambda *a, **k: _Res())

    async def _fake_vision(_p, _k):
        return _VRes()

    monkeypatch.setattr("swarm.brain.vision_ingest.understand_file_async", _fake_vision)
    monkeypatch.setattr("swarm.brain.vision_ingest.annotate_for_draft",
                        lambda v: "【图片】登录页")

    out = await ingest_node.ingest({"uploaded_files": [str(img)],
                                    "task_description": "做个登录"})
    assert out.get("ingest_vision_pending"), "前提：本用例要求产生待确认视觉项"
    assert not out.get("ingest_errors"), "前提：本用例不该有 errors"
    _dg = out.get("degraded_reasons") or []
    assert not [d for d in _dg if d.startswith("ingest_partial_failure")], (
        f"待人工确认≠失败，绝不能触发该前缀。实得 {_dg}"
    )


@pytest.mark.asyncio
async def test_vision_failure_does_produce_degraded(monkeypatch, tmp_path):
    """与上一条唯一差别＝视觉理解**失败**（图片 PRD 的主力失败面）⇒ 必须写账。

    两条合起来锁住"分档"这一维本身，而不只是"有没有写账"。
    """
    from swarm.brain import ingest_node

    img = tmp_path / "ui.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    class _Doc:
        filename = "ui.png"
        needs_vision = True
        error = None
        kind = "image"

    class _Res:
        draft_text = ""
        errors: list = []
        documents = [_Doc()]

    class _VResBad:
        ok = False
        filename = "ui.png"
        error = "vision provider 不可用"

    monkeypatch.setattr("swarm.brain.ingest.ingest_files", lambda *a, **k: _Res())

    async def _fake_vision(_p, _k):
        return _VResBad()

    monkeypatch.setattr("swarm.brain.vision_ingest.understand_file_async", _fake_vision)

    out = await ingest_node.ingest({"uploaded_files": [str(img)],
                                   "task_description": "做个登录"})
    assert out.get("ingest_errors"), "前提：视觉失败应进 errors"
    _dg = out.get("degraded_reasons") or []
    assert [d for d in _dg if d.startswith("ingest_partial_failure")], (
        f"视觉理解失败=需求可能蒸发，必须写机读账。实得 {_dg}"
    )


# ── ③ 挡 auto_accept（承重锁：本函数不通吃 degraded_reasons） ──

def _clean_state(**over):
    """一个本来能自动放行的终态（其余闸全过），只改摄取这一维。"""
    st = {
        "l2_passed": True, "l3_passed": True, "runtime_smoke_passed": True,
        "acceptance_passed": True, "failed_subtask_ids": [], "merge_owner_drops": [],
        "failure_escalated": False, "verification_failure": None,
        "verification_coverage": {"l2": "ok"}, "degraded_reasons": [],
    }
    st.update(over)
    return st


def test_baseline_clean_state_would_auto_accept():
    """前提闸：不带摄取失败时本来是放行的——否则下一条测的是别的拒因（假绿）。"""
    allow, reason = can_auto_accept_delivery(_clean_state())
    assert allow, f"夹具失效：干净态就被拒了，拒因={reason}"


def test_ingest_partial_failure_blocks_auto_accept():
    """★承重锁★ `can_auto_accept_delivery` 只认四个既有前缀，不通吃 degraded_reasons，
    所以必须有显式一臂。删掉那一臂 ⇒ 本条红。"""
    allow, reason = can_auto_accept_delivery(
        _clean_state(degraded_reasons=["ingest_partial_failure:2/3"]))
    assert not allow, "摄取部分失败必须拒绝 auto_accept（需求可能已缩水）"
    assert "ingest_partial_failure" in reason, f"拒因须如实归因，实得 {reason}"


def test_unrelated_degraded_still_auto_accepts():
    """反向锁（防闸过宽）：无关的 degraded 条目不该被这一臂拦。"""
    allow, _ = can_auto_accept_delivery(
        _clean_state(degraded_reasons=["requirements_extract:rejected=2"]))
    assert allow, "白名单类信息性留痕不该被摄取臂误拦"


# ── ④ 挡 L6 + 进人工闸 payload ──

def test_prefix_blocks_l6_success_learning():
    r = ["ingest_partial_failure:1/2"]
    assert blocking_degraded_reasons(r) == r, (
        "该前缀不在信息性白名单里 ⇒ 必须落阻断档，否则'需求缺一半仍 DONE'会被学成成功模式"
    )


def test_deliver_payload_exposes_ingest_block():
    """人工闸必须看得见摄取报告（此前 payload 无摄取块 ⇒ 人工也是盲的）。"""
    from swarm.brain.nodes import _deliver_review_payload

    payload = _deliver_review_payload({
        "ingest_errors": ["a.pdf: 解析失败: bad xref", "b.xlsx: 无解析器: .xlsx"],
        "ingest_vision_pending": [{"filename": "ui.png"}],
        "ingest_draft": "草稿正文",
    })
    blk = payload.get("ingest")
    assert isinstance(blk, dict), f"payload 必须含 ingest 块，实得键 {sorted(payload)}"
    assert blk["errors_total"] == 2, blk
    assert any("bad xref" in e for e in blk["errors"]), "须列出逐条原文（哪个文件为何没进草稿）"
    assert blk["pending_vision"] == 1, "待确认视觉与失败分开列（后果不同）"


def test_deliver_payload_ingest_block_tolerates_missing_keys():
    """旧 checkpoint 无这些键 → 空缺省，绝不抛（deliver 是 interrupt 锚点，
    payload 组装失败=人工闸打不开）。"""
    from swarm.brain.nodes import _deliver_review_payload

    blk = _deliver_review_payload({}).get("ingest")
    assert blk == {"errors": [], "errors_total": 0, "pending_vision": 0,
                   "draft_chars": 0}, blk


# ── 端到端：整层异常也要写账 ──

def test_layer_exception_writes_degraded():
    """摄取整层抛异常＝全部文件都没进草稿（最重形态），必须写账。"""
    from swarm.brain import ingest_node

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    import unittest.mock as _m
    with _m.patch.object(ingest_node, "_run_ingest", _boom):
        out = asyncio.run(ingest_node.ingest(
            {"uploaded_files": ["a.pdf", "b.pdf"], "task_description": "x"}))
    assert out.get("ingest_errors"), out
    _dg = out.get("degraded_reasons") or []
    assert any(d.startswith("ingest_partial_failure:layer_error:") for d in _dg), _dg
