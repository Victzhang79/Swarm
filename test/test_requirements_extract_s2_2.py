"""S2-2 需求条目结构化 — 行为测试（禁 getsource 结构焊死）。

覆盖面（docs/ACCEPTANCE_DESIGN.md §6 + 给 task#23 的实现指引）：
1. 纯校验函数：source_quote 回指原文 substring（空白归一）确定性校验——幻觉条目被拒；
   空文本/重复 id/超长/非法形状逐条剔除并记 rejected 原因；全军覆没→items=[]。
2. 条目 ID = 内容 hash req-<sha1[:8]>：跨空白/标点/大小写归一稳定，与顺序无关。
3. 节点：LLM 抽取+有界重试；失败如实降级 items=[] + degraded（绝不塞幻觉条目）；
   幂等跳过；PRD 截断（ingest 头70%尾30%）→ source_truncated 可观测。
4. graph 接线：contract_design→extract_requirements→plan（禁双边/无 fan-out）；
   replan 环（handle_failure/confirm→plan）不经过抽取节点。
5. SubTask.covers 加法兼容字段（旧输入无此键=默认空列表）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from swarm.brain.requirements_extract import (
    MAX_EXTRACT_RETRIES,
    MAX_ITEM_TEXT_CHARS,
    extract_requirements,
    normalize_for_id,
    requirement_id,
    source_is_truncated,
    validate_requirement_items,
)

# ───────────────────────── 纯校验函数 ─────────────────────────

_SOURCE = (
    "【任务描述】\n系统需要支持批量导入数据文件，导入失败时逐行提示错误原因。\n"
    "同时提供一个 概览 页面，展示最近一次导入的统计结果。"
)


def _raw(text: str, quote: str, kind: str = "functional", **extra) -> dict:
    return {"text": text, "kind": kind, "source_quote": quote, **extra}


def test_valid_item_accepted_with_hash_id():
    items, rejected = validate_requirement_items(
        [_raw("支持批量导入数据文件", "系统需要支持批量导入数据文件")], _SOURCE)
    assert rejected == []
    assert len(items) == 1
    it = items[0]
    assert it["text"] == "支持批量导入数据文件"
    assert it["kind"] == "functional"
    assert it["source_quote"] == "系统需要支持批量导入数据文件"
    expected = "req-" + hashlib.sha1(
        normalize_for_id("支持批量导入数据文件").encode("utf-8")).hexdigest()[:8]
    assert it["id"] == expected


def test_hallucinated_quote_rejected_others_kept():
    items, rejected = validate_requirement_items(
        [
            _raw("支持批量导入", "系统需要支持批量导入数据文件"),
            _raw("支持导出为表格", "系统需要支持导出为表格"),  # 原文没有：幻觉
        ],
        _SOURCE,
    )
    assert len(items) == 1
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "quote_not_in_source"


def test_quote_match_is_whitespace_normalized():
    # 原文里"概览 页面"带空格/换行，quote 无空白也必须命中（空白归一后比对）
    items, rejected = validate_requirement_items(
        [_raw("提供概览页面", "提供一个概览页面，展示最近一次导入的统计结果")], _SOURCE)
    assert rejected == []
    assert len(items) == 1


def test_quote_match_folds_fullwidth_halfwidth_punctuation():
    """S2 复核 S1：全半角标点互写不误杀——原文全角"，（）"，LLM quote 复述成半角
    ",()"（反向同理）；双侧同折叠后 substring 命中。"""
    source = "系统需要支持用户注册功能（含邮箱验证），注册成功后跳转首页。"
    items, rejected = validate_requirement_items(
        [_raw("支持用户注册", "支持用户注册功能(含邮箱验证),注册成功后")], source)
    assert rejected == []
    assert len(items) == 1
    # 反向：原文半角，quote 全角
    source2 = "The system must support CSV import (with row-level errors), then report."
    items2, rejected2 = validate_requirement_items(
        [_raw("支持 CSV 导入", "support CSV import （with row-level errors）， then")],
        source2)
    assert rejected2 == []
    assert len(items2) == 1


def test_quote_hallucination_still_rejected_after_punct_fold():
    """S1 边界：标点折叠只是同义归一，真幻觉（原文没有的内容）仍被拒。"""
    items, rejected = validate_requirement_items(
        [_raw("支持微信扫码登录", "系统需要支持微信扫码登录功能，")], _SOURCE)
    assert items == []
    assert rejected[0]["reason"] == "quote_not_in_source"


# ── R35-B：表格/结构化源的误杀治本（源料平铺接地 Tier2）──
# 离线实测坐实：被拒 quote 每片都是源逐字内容，只是被 markdown `|` 与跨行切断连续性。
_TABLE_SOURCE = (
    "### 预警发送接口\n"
    "| 接口 | 说明 | 场景 |\n"
    "| `/notify/simple` | 简化预警 | 传入任务ID+参数，自动编排发送 |\n"
    "| `/notify/recover` | 恢复提醒 | 预警解除后发送恢复通知 |\n"
    "### 通知用户\n"
    "| 企业微信 ID | 企业微信用户标识 |\n"
    "| 邮箱 | 邮箱地址 |\n"
    "| VoIP ID | VoIP 推送设备标识 |\n"
)


def test_table_row_cells_joined_grounded():
    """R35-B①表格竖线打断：LLM 把一行多个单元格拼成一句 quote，源里单元格间有
    markdown `|`→连续 substring 不中，但每个单元格都是源逐字内容→Tier2 平铺救回。"""
    q = "/notify/simple 简化预警 传入任务ID+参数，自动编排发送"
    items, rejected = validate_requirement_items(
        [_raw("提供 /notify/simple 简化预警接口", q)], _TABLE_SOURCE)
    assert rejected == [], f"表格行拼接应接地放行，实际被拒 {rejected}"
    assert len(items) == 1


def test_cross_row_field_list_grounded():
    """R35-B②跨行拼接：LLM 把分散在多行的字段名拼成清单，各片都在源只是不连续→Tier2 救回。"""
    q = "企业微信 ID、邮箱、VoIP ID"
    items, rejected = validate_requirement_items(
        [_raw("通知用户支持配置企微/邮箱/VoIP ID", q)], _TABLE_SOURCE)
    assert rejected == [], f"跨行字段拼接应接地放行，实际被拒 {rejected}"
    assert len(items) == 1


def test_partial_fabrication_still_rejected():
    """R35-B 防幻觉底线：真前缀 + 源里没有的编造尾 → 内容覆盖率不达标仍拒（防"半真半假"蒙混）。"""
    q = "传入任务ID+参数，自动编排发送并永久加密上传至海外云端灾备节点"
    items, rejected = validate_requirement_items([_raw("编造需求", q)], _TABLE_SOURCE)
    assert items == []
    assert rejected[0]["reason"] == "quote_not_in_source"


# 散文源：真词汇散落各处、各主题句顺序固定——用于验证"乱序重拼"编造被前向单调拒。
_PROSE_SOURCE = (
    "用户可以通过手机号码注册账号；注册时系统发送验证码到用户手机完成校验。"
    "登录后，用户可在个人中心修改邮箱与密码。"
    "管理员可以查看用户列表并导出报表。"
    "订单模块记录用户的下单与支付信息。"
)


def test_stitched_hallucination_rejected():
    """R35-B 双复核收紧（防幻觉回归锚）：把散落各处的真词汇【乱序重拼】成源里不存在的新
    主张（"管理员通过邮箱改用户密码"——源里管理员只"查看用户列表"、改密码的是用户本人），
    前向单调平铺因片顺序对不上而覆盖塌 → 必须仍判 quote_not_in_source。此前非单调贪心平铺
    会把散点 2-gram 凑到 ~0.85 蒙混过闸（复核双方独立复现）。"""
    q = "管理员可以通过邮箱修改用户密码并发送验证码通知手机号码"
    items, rejected = validate_requirement_items(
        [_raw("编造管理员改密", q)], _PROSE_SOURCE)
    assert items == [], "乱序重拼的编造 quote 必须被拒（防幻觉底线）"
    assert rejected and rejected[0]["reason"] == "quote_not_in_source"


def test_in_order_prose_multiclause_still_grounded():
    """对照：真实按源顺序的多子句拼接（去标点接缝）仍应前向平铺接地——证明收紧只砍乱序编造，
    不误伤"按序去接缝"的真 quote。"""
    q = "用户可以通过手机号码注册账号 系统发送验证码到用户手机完成校验"
    items, rejected = validate_requirement_items(
        [_raw("手机号注册+验证码校验", q)], _PROSE_SOURCE)
    assert rejected == [], f"按源顺序的真拼接应接地放行，实际被拒 {rejected}"
    assert len(items) == 1


def test_grounding_min_tile_env_tunable(monkeypatch):
    """接地阈值 env 可调（非硬编码，配置面可运维）：min_tile 调到极大→无片可平铺→
    表格拼接被判不接地而拒，证明参数真接线到判定。"""
    monkeypatch.setenv("SWARM_QUOTE_MIN_TILE_CHARS", "30")
    q = "企业微信 ID、邮箱、VoIP ID"
    items, rejected = validate_requirement_items(
        [_raw("通知用户配置", q)], _TABLE_SOURCE)
    assert rejected and rejected[0]["reason"] == "quote_not_in_source"


def test_empty_text_and_empty_quote_rejected():
    items, rejected = validate_requirement_items(
        [
            _raw("", "系统需要支持批量导入数据文件"),
            _raw("有文本但无出处", ""),
        ],
        _SOURCE,
    )
    assert items == []
    reasons = {r["reason"] for r in rejected}
    assert "empty_text" in reasons
    assert "empty_quote" in reasons


def test_duplicate_id_keeps_first_rejects_rest():
    # 两条文本仅空白/标点不同 → 归一化后同 hash → 后者判 duplicate
    items, rejected = validate_requirement_items(
        [
            _raw("支持批量导入数据文件", "系统需要支持批量导入数据文件"),
            _raw("支持批量导入 数据文件。", "系统需要支持批量导入数据文件"),
        ],
        _SOURCE,
    )
    assert len(items) == 1
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "duplicate"


def test_overlong_text_rejected():
    long_text = "导入" * (MAX_ITEM_TEXT_CHARS // 2 + 10)
    items, rejected = validate_requirement_items(
        [_raw(long_text, "系统需要支持批量导入数据文件")], _SOURCE)
    assert items == []
    assert rejected[0]["reason"] == "too_long"


def test_non_dict_entries_rejected_not_crash():
    items, rejected = validate_requirement_items(
        ["just a string", 42, None], _SOURCE)
    assert items == []
    assert all(r["reason"] == "not_object" for r in rejected)


def test_all_rejected_returns_empty_list():
    items, rejected = validate_requirement_items(
        [_raw("凭空捏造的需求", "原文里根本不存在的引用")], _SOURCE)
    assert items == []
    assert len(rejected) == 1


def test_unknown_kind_coerced_to_other_known_aliases_normalized():
    items, _ = validate_requirement_items(
        [
            _raw("条目甲：批量导入", "支持批量导入数据文件", kind="接口"),
            _raw("条目乙：概览页面", "提供一个概览页面", kind="奇怪分类"),
        ],
        _SOURCE,
    )
    assert items[0]["kind"] == "api"
    assert items[1]["kind"] == "other"


def test_id_stable_across_case_whitespace_punct_and_order():
    a = requirement_id("Import CSV files, in batch!")
    b = requirement_id("import   csv files in batch")
    assert a == b
    assert a.startswith("req-") and len(a) == len("req-") + 8
    # 顺序无关：同一批条目换序，各条目 ID 不变
    batch1, _ = validate_requirement_items(
        [_raw("支持批量导入", "系统需要支持批量导入数据文件"),
         _raw("提供概览页面", "提供一个概览页面")], _SOURCE)
    batch2, _ = validate_requirement_items(
        [_raw("提供概览页面", "提供一个概览页面"),
         _raw("支持批量导入", "系统需要支持批量导入数据文件")], _SOURCE)
    assert {i["id"] for i in batch1} == {i["id"] for i in batch2}


def test_source_field_sanitized():
    items, _ = validate_requirement_items(
        [_raw("支持批量导入", "系统需要支持批量导入数据文件", source="clarify"),
         _raw("提供概览页面", "提供一个概览页面", source="/etc/passwd")], _SOURCE)
    assert items[0]["source"] == "clarify"
    assert items[1]["source"] == "description"  # 非法取值回落默认


# ───────────────────────── 截断可观测（与 ingest 行为对齐）─────────────────────────

def test_truncation_detector_matches_real_ingest_output():
    """行为锁定：source_is_truncated 必须认得 summarize_to_budget 真实产出的截断标记。"""
    from swarm.brain.ingest import summarize_to_budget

    truncated_text, was_truncated = summarize_to_budget("需求" * 100_000, 100)
    assert was_truncated is True
    assert source_is_truncated(truncated_text) is True

    short_text, was_truncated2 = summarize_to_budget("很短的需求", 100)
    assert was_truncated2 is False
    assert source_is_truncated(short_text) is False


# ───────────────────────── 节点（stub LLM）─────────────────────────

class _StubLLM:
    """按序回放 content 的 LLM stub；record 调用次数。content 为 Exception 时抛出。"""

    def __init__(self, contents: list):
        self.contents = list(contents)
        self.calls: list[list] = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        item = self.contents.pop(0) if self.contents else self.contents_exhausted()
        if isinstance(item, Exception):
            raise item
        return type("R", (), {"content": item})()

    def contents_exhausted(self):
        return json.dumps({"items": []})


def _wire_llm(monkeypatch, stub: _StubLLM) -> None:
    import swarm.brain.nodes as nodes_pkg

    monkeypatch.setattr(nodes_pkg, "_get_brain_llm", lambda: stub)


def _good_payload() -> str:
    return json.dumps({"items": [
        {"text": "支持批量导入数据文件", "kind": "功能",
         "source_quote": "系统需要支持批量导入数据文件"},
        {"text": "提供导入结果概览页面", "kind": "页面",
         "source_quote": "提供一个概览页面"},
    ]}, ensure_ascii=False)


def _run(state: dict) -> dict:
    return asyncio.run(extract_requirements(state))


def test_node_happy_path_writes_requirement_items(monkeypatch):
    stub = _StubLLM([_good_payload()])
    _wire_llm(monkeypatch, stub)
    out = _run({"task_description": _SOURCE})
    items = out["requirement_items"]
    assert len(items) == 2
    assert all(i["id"].startswith("req-") for i in items)
    assert {i["kind"] for i in items} == {"functional", "page"}
    assert all(not i.get("source_truncated") for i in items)
    assert len(stub.calls) == 1
    # 干净路径不额外留降级痕
    assert "degraded_reasons" not in out


def test_node_partial_rejection_keeps_valid_and_records_degraded(monkeypatch):
    payload = json.dumps({"items": [
        {"text": "支持批量导入数据文件", "kind": "functional",
         "source_quote": "系统需要支持批量导入数据文件"},
        {"text": "编造的需求", "kind": "functional", "source_quote": "原文没有这句话"},
    ]}, ensure_ascii=False)
    stub = _StubLLM([payload])
    _wire_llm(monkeypatch, stub)
    out = _run({"task_description": _SOURCE})
    assert len(out["requirement_items"]) == 1
    assert any("rejected" in r for r in out["degraded_reasons"])


def test_node_retries_then_succeeds(monkeypatch):
    stub = _StubLLM(["not json at all {{{", _good_payload()])
    _wire_llm(monkeypatch, stub)
    out = _run({"task_description": _SOURCE})
    assert len(out["requirement_items"]) == 2
    assert len(stub.calls) == 2


def test_node_retries_exhausted_degrades_to_empty_never_hallucinates(monkeypatch):
    bad = json.dumps({"items": [
        {"text": "编造需求", "kind": "functional", "source_quote": "原文没有"}]})
    stub = _StubLLM([bad] * (1 + MAX_EXTRACT_RETRIES + 5))
    _wire_llm(monkeypatch, stub)
    out = _run({"task_description": _SOURCE})
    assert out["requirement_items"] == []
    assert len(stub.calls) == 1 + MAX_EXTRACT_RETRIES  # 有界：首发 + MAX 次重试
    assert any("requirements_extract" in r for r in out["degraded_reasons"])


def test_node_llm_unavailable_raises_failloud(monkeypatch):
    """R38-D 行为变更（round38 拍板）：LLM 全挂 + 源非空 → raise fail-loud，
    不再静默降级空清单（需求分母清零=覆盖闸失去牙，会带 0 需求"全覆盖"交付）。
    旧契约"degrades_to_empty"仅保留给空源/全被拒两类（见 test_r38d_extract_failloud）。"""
    stub = _StubLLM([RuntimeError("llm down")] * (1 + MAX_EXTRACT_RETRIES))
    _wire_llm(monkeypatch, stub)
    with pytest.raises(RuntimeError, match="EXTRACT_REQ"):
        _run({"task_description": _SOURCE})


def test_node_idempotent_skip_when_items_exist(monkeypatch):
    stub = _StubLLM([_good_payload()])
    _wire_llm(monkeypatch, stub)
    out = _run({
        "task_description": _SOURCE,
        "requirement_items": [{"id": "req-deadbeef", "text": "已有条目"}],
    })
    assert out == {}          # 不重抽、不重烧 LLM
    assert stub.calls == []


def test_node_empty_source_degrades_without_llm_call(monkeypatch):
    stub = _StubLLM([_good_payload()])
    _wire_llm(monkeypatch, stub)
    out = _run({"task_description": "   "})
    assert out["requirement_items"] == []
    assert stub.calls == []
    assert any("empty_source" in r for r in out["degraded_reasons"])


def test_node_truncated_source_marks_items_and_degraded(monkeypatch):
    from swarm.brain.ingest import summarize_to_budget

    truncated_desc, _ = summarize_to_budget(
        _SOURCE + "\n" + ("补充需求细节。" * 100_000), 200)
    payload = json.dumps({"items": [
        {"text": "支持批量导入数据文件", "kind": "functional",
         "source_quote": "系统需要支持批量导入数据文件"}]}, ensure_ascii=False)
    stub = _StubLLM([payload])
    _wire_llm(monkeypatch, stub)
    out = _run({"task_description": truncated_desc})
    assert out["requirement_items"][0]["source_truncated"] is True
    assert any("source_truncated" in r for r in out["degraded_reasons"])


def test_node_clarify_summary_counts_as_quote_source(monkeypatch):
    payload = json.dumps({"items": [
        {"text": "导入上限一万行", "kind": "data",
         "source_quote": "单次导入上限为一万行", "source": "clarify"}]},
        ensure_ascii=False)
    stub = _StubLLM([payload])
    _wire_llm(monkeypatch, stub)
    out = _run({
        "task_description": _SOURCE,
        "clarify_summary": "第1轮澄清：用户答复单次导入上限为一万行。",
    })
    assert len(out["requirement_items"]) == 1
    assert out["requirement_items"][0]["source"] == "clarify"


# ───────────────────────── graph 接线 ─────────────────────────

def test_graph_wiring_contract_design_to_extract_to_plan():
    from swarm.brain.graph import build_brain_graph

    graph = build_brain_graph()
    assert "extract_requirements" in graph.nodes
    assert ("contract_design", "extract_requirements") in graph.edges
    assert ("extract_requirements", "plan") in graph.edges
    # 禁双边：旧直连边必须移除，否则 fan-out 并行触发（confirm 血案同款）
    assert ("contract_design", "plan") not in graph.edges
    # 抽取节点是纯静态直通：不挂条件边、唯一出口 plan
    assert "extract_requirements" not in graph.branches
    static_targets = {dst for (src, dst) in graph.edges if src == "extract_requirements"}
    assert static_targets == {"plan"}


def test_replan_loops_bypass_extract_node():
    """取证结论锁定（ACCEPTANCE_DESIGN §6.4）：replan 环不回到抽取节点——
    handle_failure→plan 与 confirm(REVISE)→plan 都直指 plan，requirement_items
    一次生成后天然稳定，无每次 replan 重烧 LLM 的风险。"""
    from swarm.brain.graph import build_brain_graph

    graph = build_brain_graph()
    for src in ("handle_failure", "confirm"):
        ends: dict = {}
        for spec in graph.branches[src].values():
            ends.update(spec.ends or {})
        assert ends.get("plan") == "plan", f"{src} 的 replan 出口必须直指 plan"


# ───────────────────────── SubTask.covers 加法兼容 ─────────────────────────

def test_subtask_covers_defaults_empty_and_roundtrips():
    from swarm.types import FileScope, SubTask

    # 旧输入（无 covers 键）→ 默认空列表，绝不 KeyError（旧 checkpoint 兼容）
    st_old = SubTask.model_validate(
        {"id": "st-1", "description": "x", "scope": {"create_files": ["a.py"]}})
    assert st_old.covers == []

    st_new = SubTask(id="st-2", description="y",
                     scope=FileScope(create_files=["b.py"]),
                     covers=["req-deadbeef"])
    assert st_new.covers == ["req-deadbeef"]
    assert SubTask.model_validate(st_new.model_dump()).covers == ["req-deadbeef"]


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
