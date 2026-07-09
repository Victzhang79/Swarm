#!/usr/bin/env python3
"""S2-4（task#24 前置）：验收断言 spec 纯模块 —— 行为测试（禁 getsource）。

覆盖面（对齐 docs/ACCEPTANCE_DESIGN.md 定案 2 / §5 / 给 task#25 的实现指引）：
- schema 校验正反例：悬空 req_id 剔除、path 带 scheme/host 拒绝（SSRF 面）、
  auth 缺省 "manual"（fail-closed）、枚举外 kind/method 拒绝、重复 id 去重、
  body_json 不可序列化拒绝、header CRLF 注入拒绝、status 非法码拒绝；
- probe cmd 生成：含 body 的 POST base64 往返、shlex 注入样例（path 含 ;rm）、
  `__ACCEPT_RESULT__`/`__ACCEPT_BODY__` 标记完整性、auth!="none" 拒绝生成（fail-closed）；
- 判定纯函数：status 命中/不中、body_contains 命中/不中、manual 绝不判 pass；
- prompt builder：防臆造纪律（要求 evidence 依据）、manual 降级纪律、条目注入——
  对产物文本断言（纯文本产物，允许）。

全部纯函数，零网络零沙箱 IO。
"""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import shlex
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.acceptance_spec import (  # noqa: E402
    ACCEPT_BODY_HEAD_BYTES,
    DEFAULT_PROBE_MAX_TIME_SEC,
    MARK_ACCEPT_BODY,
    MARK_ACCEPT_RESULT,
    assertion_to_probe_cmd,
    build_assertion_generation_prompt,
    evaluate_probe_result,
    parse_probe_output,
    validate_assertions,
)

# ═══════════════════ fixtures（纯数据） ═══════════════════

REQ_ITEMS = [
    {"id": "req-aaaa1111", "text": "系统提供健康检查接口", "source_quote": "健康检查"},
    {"id": "req-bbbb2222", "text": "支持创建业务记录", "source_quote": "创建记录"},
]


def _valid_spec(**over):
    spec = {
        "id": "a1",
        "req_id": "req-aaaa1111",
        "kind": "http_probe",
        "request": {"method": "GET", "path": "/actuator/health"},
        "expect": {"status": [200]},
        "auth": "none",
    }
    spec.update(over)
    return spec


# ═══════════════════ schema 校验：正例 ═══════════════════

def test_valid_http_probe_accepted_and_normalized():
    valid, rejected = validate_assertions([_valid_spec()], REQ_ITEMS)
    assert rejected == []
    assert len(valid) == 1
    s = valid[0]
    assert s["id"] == "a1" and s["req_id"] == "req-aaaa1111"
    assert s["kind"] == "http_probe" and s["auth"] == "none"
    assert s["request"]["method"] == "GET"
    assert s["expect"]["status"] == [200]


def test_method_lowercase_normalized_and_single_status_listified():
    raw = _valid_spec()
    raw["request"] = {"method": "post", "path": "/api/items",
                      "body_json": {"name": "x"}}
    raw["expect"] = {"status": 201}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert rejected == []
    assert valid[0]["request"]["method"] == "POST"
    assert valid[0]["expect"]["status"] == [201]


def test_manual_kind_accepted_without_request():
    raw = {"id": "m1", "req_id": "req-bbbb2222", "kind": "manual"}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert rejected == []
    assert valid[0]["kind"] == "manual"
    assert valid[0]["auth"] == "manual"  # 缺省 fail-closed


def test_evidence_passthrough_preserved():
    raw = _valid_spec(evidence="设计文档：GET /actuator/health 返回 200")
    valid, _ = validate_assertions([raw], REQ_ITEMS)
    assert "actuator/health" in valid[0]["evidence"]


# ═══════════════════ schema 校验：反例（fail-closed） ═══════════════════

def test_auth_missing_defaults_to_manual():
    raw = _valid_spec()
    raw.pop("auth")
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert rejected == []
    assert valid[0]["auth"] == "manual"


def test_auth_unknown_value_coerced_to_manual():
    # 语义演进（阶段6 D8①）：bearer 成为合法 auth（冒烟登录取 token 可执行）；
    # 未知值仍 fail-closed 降 manual。
    valid, _ = validate_assertions([_valid_spec(auth="bearer")], REQ_ITEMS)
    assert valid[0]["auth"] == "bearer"
    valid2, _ = validate_assertions([_valid_spec(auth="kerberos")], REQ_ITEMS)
    assert valid2[0]["auth"] == "manual"


def test_dangling_req_id_rejected_with_reason():
    valid, rejected = validate_assertions(
        [_valid_spec(req_id="req-deadbeef")], REQ_ITEMS)
    assert valid == []
    assert len(rejected) == 1
    assert "req_id" in rejected[0]["reason"]


def test_kind_outside_enum_rejected():
    valid, rejected = validate_assertions(
        [_valid_spec(kind="sql_probe")], REQ_ITEMS)
    assert valid == []
    assert len(rejected) == 1 and "kind" in rejected[0]["reason"]


def test_method_outside_enum_rejected():
    raw = _valid_spec()
    raw["request"] = {"method": "TRACE", "path": "/x"}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert valid == [] and len(rejected) == 1
    assert "method" in rejected[0]["reason"]


@pytest.mark.parametrize("bad_path", [
    "http://evil.example/x",       # 带 scheme
    "//evil.example/x",            # protocol-relative host
    "/a/b://c",                    # 内嵌 scheme 分隔符
    "relative/path",               # 不以 / 开头
    "/has space",                  # 空白
    "",                            # 空
])
def test_path_with_host_or_malformed_rejected(bad_path):
    raw = _valid_spec()
    raw["request"] = {"method": "GET", "path": bad_path}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert valid == []
    assert len(rejected) == 1 and "path" in rejected[0]["reason"]


def test_invalid_status_code_rejected():
    for bad in ([999], ["200"], [True]):
        raw = _valid_spec()
        raw["expect"] = {"status": bad}
        valid, rejected = validate_assertions([raw], REQ_ITEMS)
        assert valid == [], bad
        assert "status" in rejected[0]["reason"]


def test_body_json_unserializable_rejected():
    raw = _valid_spec()
    raw["request"] = {"method": "POST", "path": "/x", "body_json": {"k": object()}}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert valid == [] and "body_json" in rejected[0]["reason"]


def test_header_crlf_injection_rejected():
    raw = _valid_spec()
    raw["request"] = {"method": "GET", "path": "/x",
                      "headers": {"X-Evil": "a\r\nHost: evil"}}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert valid == [] and "header" in rejected[0]["reason"].lower()


def test_duplicate_id_deduped_first_wins():
    a = _valid_spec()
    b = _valid_spec()
    b["request"] = {"method": "GET", "path": "/other"}
    valid, rejected = validate_assertions([a, b], REQ_ITEMS)
    assert len(valid) == 1
    assert valid[0]["request"]["path"] == "/actuator/health"  # 先到者胜
    assert len(rejected) == 1 and "重复" in rejected[0]["reason"]


def test_id_with_shell_metachars_rejected():
    valid, rejected = validate_assertions(
        [_valid_spec(id="a1; rm -rf /")], REQ_ITEMS)
    assert valid == [] and "id" in rejected[0]["reason"]


def test_non_list_input_rejected_wholesale():
    valid, rejected = validate_assertions({"not": "a list"}, REQ_ITEMS)
    assert valid == [] and len(rejected) == 1


def test_non_dict_entry_rejected_others_survive():
    valid, rejected = validate_assertions(["garbage", _valid_spec()], REQ_ITEMS)
    assert len(valid) == 1 and len(rejected) == 1


# ═══════════════════ probe cmd 生成 ═══════════════════

def test_get_probe_cmd_markers_and_url():
    spec = validate_assertions([_valid_spec()], REQ_ITEMS)[0][0]
    cmd = assertion_to_probe_cmd(spec, 8080)
    assert f"{MARK_ACCEPT_RESULT}a1__" in cmd
    assert f"{MARK_ACCEPT_BODY}a1__" in cmd
    assert shlex.quote("http://127.0.0.1:8080/actuator/health") in cmd
    assert f"--max-time {DEFAULT_PROBE_MAX_TIME_SEC}" in cmd
    assert "curl -s -o" in cmd
    assert str(ACCEPT_BODY_HEAD_BYTES) in cmd  # body 截断收割


def test_post_body_base64_roundtrip_and_content_type():
    body = {"name": "it's \"quoted\"", "n": 3, "中文": "值"}
    raw = _valid_spec(id="a2", req_id="req-bbbb2222")
    raw["request"] = {"method": "POST", "path": "/api/items", "body_json": body}
    raw["expect"] = {"status": [200, 201]}
    spec = validate_assertions([raw], REQ_ITEMS)[0][0]
    cmd = assertion_to_probe_cmd(spec, 9000)
    # base64 往返：从 cmd 中抽出 b64 payload，解码必须回到同一 JSON 对象
    m = re.search(r"printf %s (\S+) \| base64 -d", cmd)
    assert m, cmd
    b64 = shlex.split(m.group(1))[0]
    assert json.loads(base64.b64decode(b64).decode("utf-8")) == body
    assert "-X POST" in cmd
    assert shlex.quote("Content-Type: application/json") in cmd
    assert "--data @" in cmd


def test_explicit_content_type_not_duplicated():
    raw = _valid_spec(id="a3")
    raw["request"] = {"method": "POST", "path": "/x", "body_json": {"a": 1},
                      "headers": {"Content-Type": "application/vnd.custom+json"}}
    spec = validate_assertions([raw], REQ_ITEMS)[0][0]
    cmd = assertion_to_probe_cmd(spec, 8080)
    assert cmd.count("Content-Type") == 1
    assert "vnd.custom+json" in cmd


def test_probe_cmd_shlex_quotes_injection_path():
    raw = _valid_spec(id="a4")
    raw["request"] = {"method": "GET", "path": "/a;rm$(reboot)"}
    spec = validate_assertions([raw], REQ_ITEMS)[0][0]
    cmd = assertion_to_probe_cmd(spec, 8080)
    # 注入面必须整体在 shlex.quote 产物内，绝不裸露
    assert shlex.quote("http://127.0.0.1:8080/a;rm$(reboot)") in cmd
    assert ";rm$(reboot)'" in cmd and "\n;rm" not in cmd


def test_probe_cmd_header_quoted():
    raw = _valid_spec(id="a5")
    raw["request"] = {"method": "GET", "path": "/x",
                      "headers": {"X-Probe": "v;`id`"}}
    spec = validate_assertions([raw], REQ_ITEMS)[0][0]
    cmd = assertion_to_probe_cmd(spec, 8080)
    assert "-H " + shlex.quote("X-Probe: v;`id`") in cmd


def test_probe_cmd_refuses_manual_auth_fail_closed():
    spec = validate_assertions([_valid_spec(auth="manual")], REQ_ITEMS)[0][0]
    with pytest.raises(ValueError):
        assertion_to_probe_cmd(spec, 8080)


def test_probe_cmd_refuses_manual_kind_and_bad_port():
    manual = validate_assertions(
        [{"id": "m1", "req_id": "req-aaaa1111", "kind": "manual"}], REQ_ITEMS)[0][0]
    with pytest.raises(ValueError):
        assertion_to_probe_cmd(manual, 8080)
    spec = validate_assertions([_valid_spec()], REQ_ITEMS)[0][0]
    for bad_port in (0, -1, 70000, "abc"):
        with pytest.raises(ValueError):
            assertion_to_probe_cmd(spec, bad_port)


def test_parse_probe_output_roundtrip():
    body = "hello 中文 body"
    b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    out = (
        "noise line\n"
        f"{MARK_ACCEPT_RESULT}a1__200\n"
        f"{MARK_ACCEPT_BODY}a1__{b64}\n"
        f"{MARK_ACCEPT_RESULT}a2__000\n"
        f"{MARK_ACCEPT_BODY}a2__\n"
    )
    parsed = parse_probe_output(out)
    assert parsed["a1"]["http_code"] == 200
    assert parsed["a1"]["body_text"] == body
    assert parsed["a2"]["http_code"] == 0
    assert parsed["a2"]["body_text"] == ""


def test_parse_probe_output_bad_b64_degrades_to_empty():
    out = f"{MARK_ACCEPT_RESULT}a1__200\n{MARK_ACCEPT_BODY}a1__!!!not-b64!!!\n"
    parsed = parse_probe_output(out)
    assert parsed["a1"]["http_code"] == 200
    assert parsed["a1"]["body_text"] == ""


# ═══════════════════ 判定纯函数 ═══════════════════

def _spec_with_expect(**expect):
    raw = _valid_spec()
    raw["expect"] = {"status": [200, 201], **expect}
    return validate_assertions([raw], REQ_ITEMS)[0][0]


def test_evaluate_status_hit():
    res = evaluate_probe_result(_spec_with_expect(), 201, "whatever")
    assert res["passed"] is True


def test_evaluate_status_miss_with_reason():
    res = evaluate_probe_result(_spec_with_expect(), 404, "")
    assert res["passed"] is False
    assert "404" in res["reason"] and "200" in res["reason"]


def test_evaluate_body_contains_hit_and_miss():
    spec = _spec_with_expect(body_contains=["\"status\":\"UP\""])
    ok = evaluate_probe_result(spec, 200, "{\"status\":\"UP\"}")
    assert ok["passed"] is True
    miss = evaluate_probe_result(spec, 200, "{\"status\":\"DOWN\"}")
    assert miss["passed"] is False and "body" in miss["reason"].lower()


def test_evaluate_no_http_code_inconclusive_never_pass():
    """F6 语义修正：无有效 HTTP 应答（000/超时/连接失败）是 infra 不确定形态 →
    passed=None（inconclusive），非确定性 False（原意图【绝不判 pass】保留，
    只是不再把 infra 冤枉成断言失败）。"""
    for code in (None, 0, "000"):
        res = evaluate_probe_result(_spec_with_expect(), code, "")
        assert res["passed"] is None, f"code={code!r} 应 inconclusive"
        assert res["passed"] is not True
        assert "infra" in res["reason"] or "连接失败" in res["reason"]


def test_evaluate_refuses_manual_never_green():
    manual = validate_assertions(
        [{"id": "m1", "req_id": "req-aaaa1111", "kind": "manual"}], REQ_ITEMS)[0][0]
    res = evaluate_probe_result(manual, 200, "ok")
    assert res["passed"] is False
    auth_manual = validate_assertions([_valid_spec(auth="manual")], REQ_ITEMS)[0][0]
    res2 = evaluate_probe_result(auth_manual, 200, "ok")
    assert res2["passed"] is False


# ═══════════════════ prompt builder（对产物文本断言） ═══════════════════

def test_prompt_contains_items_and_context():
    prompt = build_assertion_generation_prompt(REQ_ITEMS, "设计上下文：GET /health")
    for item in REQ_ITEMS:
        assert item["id"] in prompt
        assert item["text"] in prompt
    assert "设计上下文：GET /health" in prompt
    assert "http_probe" in prompt and "manual" in prompt


def test_prompt_has_anti_hallucination_discipline():
    prompt = build_assertion_generation_prompt(REQ_ITEMS, "")
    assert "臆造" in prompt                    # 绝不臆造不存在的 API 路径
    assert "evidence" in prompt                # 每条断言给出依据
    assert "依据" in prompt or "证据" in prompt


def test_prompt_has_auth_fail_closed_discipline():
    prompt = build_assertion_generation_prompt(REQ_ITEMS, "")
    assert "auth" in prompt
    assert "none" in prompt
    # 鉴权条目降级 manual 的纪律必须写明
    assert "鉴权" in prompt or "登录" in prompt


def test_prompt_has_side_effect_independence_discipline():
    """S1（S3 项）：断言副作用序纪律——相互独立/不对同一唯一资源重复写入/
    读断言不依赖写断言执行顺序。"""
    prompt = build_assertion_generation_prompt(REQ_ITEMS, "")
    assert "相互独立" in prompt
    assert "重复写入" in prompt
    assert "执行顺序" in prompt


# ═══════════════════ F7：evidence grounding（context_text 确定性对账） ═══════════════════

_CTX = "接口定义：\n  GET /actuator/health 返回 200\n  POST /api/items 创建记录"


def test_grounding_no_evidence_coerced_to_manual_with_trace():
    raw = _valid_spec()
    raw.pop("evidence", None)
    valid, rejected = validate_assertions([raw], REQ_ITEMS, context_text=_CTX)
    assert len(valid) == 1, "降级保留（人工可见），不剔除"
    assert valid[0]["kind"] == "manual" and valid[0]["auth"] == "manual"
    assert valid[0]["request"]["path"] == "/actuator/health", "request 保留供人工核验"
    assert len(rejected) == 1 and "evidence" in rejected[0]["reason"]


def test_grounding_fabricated_evidence_coerced_to_manual():
    raw = _valid_spec(evidence="设计文档写明 GET /made/up/path 返回 200")  # 语料里没有
    valid, rejected = validate_assertions([raw], REQ_ITEMS, context_text=_CTX)
    assert valid[0]["kind"] == "manual"
    assert len(rejected) == 1


def test_grounding_real_evidence_whitespace_normalized_passes():
    # 语料里该行带换行/缩进，evidence 单行复述——空白归一后必须命中
    raw = _valid_spec(evidence="GET /actuator/health 返回 200")
    valid, rejected = validate_assertions([raw], REQ_ITEMS, context_text=_CTX)
    assert rejected == []
    assert valid[0]["kind"] == "http_probe" and valid[0]["auth"] == "none"


def test_grounding_backward_compat_without_context():
    # 不传 context_text 维持旧行为：无 evidence 的 http_probe 照常放行
    raw = _valid_spec()
    raw.pop("evidence", None)
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert rejected == []
    assert valid[0]["kind"] == "http_probe"


# ═══════════════════ F8：条数/体积上限 ═══════════════════

def test_assertion_count_capped_with_trace():
    from swarm.brain.acceptance_spec import MAX_ASSERTIONS
    raws = [{"id": f"m{i}", "req_id": "req-aaaa1111", "kind": "manual"}
            for i in range(MAX_ASSERTIONS + 5)]
    valid, rejected = validate_assertions(raws, REQ_ITEMS)
    assert len(valid) == MAX_ASSERTIONS, "超量截断"
    assert len(rejected) == 5
    # 语义演进（阶段6 D8②）：截断改分桶轮转，留痕措辞随之更新（"总帽…分桶轮转"）
    assert all(("上限" in r["reason"]) or ("总帽" in r["reason"]) for r in rejected), "截断必须留痕"


def test_oversized_body_json_coerced_to_manual():
    from swarm.brain.acceptance_spec import MAX_BODY_JSON_BYTES
    raw = _valid_spec(id="big1")
    raw["request"] = {"method": "POST", "path": "/api/items",
                      "body_json": {"blob": "x" * (MAX_BODY_JSON_BYTES + 100)}}
    valid, rejected = validate_assertions([raw], REQ_ITEMS)
    assert len(valid) == 1
    assert valid[0]["kind"] == "manual" and valid[0]["auth"] == "manual"
    assert len(rejected) == 1 and "body_json" in rejected[0]["reason"]


# ═══════════════════ F6：HEAD 用 --head ═══════════════════

def test_head_method_uses_curl_head_flag_not_x():
    raw = _valid_spec(id="h1")
    raw["request"] = {"method": "HEAD", "path": "/actuator/health"}
    spec = validate_assertions([raw], REQ_ITEMS)[0][0]
    cmd = assertion_to_probe_cmd(spec, 8080)
    assert "--head" in cmd, "-X HEAD 会等 body 直到超时（000 误判面），须用 --head"
    assert "-X HEAD" not in cmd
    # 其余 method 不受影响（回归）
    cmd_get = assertion_to_probe_cmd(
        validate_assertions([_valid_spec()], REQ_ITEMS)[0][0], 8080)
    assert "-X GET" in cmd_get


# ═══════════════════ F9：body 回显注入——首标记占位后到不覆盖 ═══════════════════

def test_parse_probe_output_forged_late_body_marker_cannot_override():
    forged = base64.b64encode(b'{"status":"UP-FORGED"}').decode("ascii")
    out = (
        f"{MARK_ACCEPT_RESULT}a1__200\n"
        f"{MARK_ACCEPT_BODY}a1__\n"            # 真标记：空 body（b64 空串也占位）
        f"{MARK_ACCEPT_BODY}a1__{forged}\n"    # 应用回显/日志里伪造的后到行
        f"{MARK_ACCEPT_RESULT}a1__500\n"       # 伪造 result 行同样不覆盖首个
    )
    parsed = parse_probe_output(out)
    assert parsed["a1"]["body_text"] == "", "首标记（空 b64）占位，伪造行不得接管"
    assert parsed["a1"]["http_code"] == 200
