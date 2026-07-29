"""26 号文 Q 路第二批：I-H1/I-H2 接口断言可自证 + I-H3 L2 契约对账三重失效。

北极星的第四道确定性闸是"接口断言"。这三条合起来的效果是：**它能被写成一定过**，
而交付报告里它显示为"已验证通过"。
"""
from __future__ import annotations

import pytest

from swarm.brain.acceptance_spec import _validate_status_list, validate_assertions
from swarm.brain.integration_review import check_contract_in_diff

# ══════════════════════════════════════════════
# I-H1：断言可被 LLM 写成"一定过"
# ══════════════════════════════════════════════


@pytest.mark.parametrize("raw,label", [
    ([200, 404], "含 404"),
    ([200, 500], "含 500"),
    ([401], "纯 4xx"),
    (list(range(200, 300)), "整段 2xx"),
    ([200, 201, 202, 204, 206], "五个码"),
])
def test_always_true_status_expectation_is_rejected(raw, label):
    """★`status=[200,…,500]` 能过校验，随后接口真返回 404 也判 passed（26 号文 I-H1）★
    一条恒真断言等于这条需求根本没被验证，而交付报告里它显示"已通过"。
    两条确定性约束：错误码不得作为成功期待；期待集不得过宽。"""
    assert _validate_status_list(raw) is None, f"{label} 应被拒"


@pytest.mark.parametrize("raw", [[200], [200, 201], 200, [302], [204]])
def test_legitimate_status_expectations_still_pass(raw):
    """闸不能矫枉过正：正常的成功码组合照常放行（含裸 int 与 3xx 重定向）。"""
    assert _validate_status_list(raw)


def test_error_handling_assertions_go_manual_not_silently_dropped():
    """真要验错误处理（"未授权返回 401"）不是不能验——走 kind=manual 交人工。
    本通道只做正向可用性验证；被拒条目带原因进 rejected，绝不静默丢。"""
    reqs = [{"id": "req-1", "text": "鉴权"}]
    valid, rejected = validate_assertions(
        [{"id": "a1", "req_id": "req-1", "kind": "http_probe",
          "request": {"method": "GET", "path": "/api/x"},
          "expect": {"status": [401]}, "auth": "none"}], reqs)
    assert not valid
    assert rejected and "4xx/5xx" in rejected[0]["reason"]


# ══════════════════════════════════════════════
# I-H2：F7 防臆造完全不约束 path
# ══════════════════════════════════════════════

_CTX = "系统提供 GET /api/users 查询接口，返回用户列表。另有 POST /api/login 登录。"
_REQS = [{"id": "req-1", "text": "用户查询"}]


def _spec(path):
    return [{"id": "a1", "req_id": "req-1", "kind": "http_probe",
             "evidence": "系统提供 GET /api/users 查询接口",
             "request": {"method": "GET", "path": path},
             "expect": {"status": [200]}, "auth": "none"}]


def test_fabricated_path_is_demoted_even_with_real_evidence():
    """★防臆造闸防的恰恰是"路径是编的"，却唯独不校验路径（26 号文 I-H2）★
    实测 `path="/totally/made/up/endpoint"` + evidence 取语料里任意一句真话即可通过。"""
    valid, rejected = validate_assertions(
        _spec("/totally/made/up/endpoint"), _REQS, _CTX)
    assert valid[0]["kind"] == "manual", "臆造路径必须降级 manual，绝不自动判过"
    assert rejected and "未在生成语料中出现" in rejected[0]["reason"]


def test_grounded_path_still_probes():
    """回指得上的真路径照常自动探测（不牺牲有效面）。"""
    valid, _ = validate_assertions(_spec("/api/users"), _REQS, _CTX)
    assert valid[0]["kind"] == "http_probe"


def test_split_annotation_path_is_not_false_flagged():
    """★判据必须是【路径段】而不是整条路径，否则误杀主流写法★
    Spring 的 `@RequestMapping("/api")` + `@GetMapping("/ping")` 拆分注解下，
    `/api/ping` 这个完整字面量在 diff 里根本不存在；Django urlpatterns 前缀嵌套、
    Express `app.use('/api', router)` 同理。要求整条路径出现＝把这些全判臆造。"""
    ctx = ('系统提供 GET /api/users 查询接口。'
           '@RestController @RequestMapping("/api") class C { @GetMapping("/ping") }')
    valid, _ = validate_assertions(_spec("/api/ping"), _REQS, ctx)
    assert valid[0]["kind"] == "http_probe"


def test_all_generic_segments_path_is_lenient():
    """路径段全是泛词（`/api/v1`）→ 无从判别，放行。
    宁可漏一个也不误杀合法断言；臆造的路径通常带业务词，本形态不像臆造。"""
    from swarm.brain.acceptance_spec import _path_grounded
    assert _path_grounded("/api/v1", "毫不相干的语料")


def test_no_context_keeps_backward_compatible():
    """不提供 context_text 时维持旧行为（向后兼容，既有调用点零回归）。"""
    valid, _ = validate_assertions(_spec("/whatever"), _REQS)
    assert valid[0]["kind"] == "http_probe"


# ══════════════════════════════════════════════
# I-H3：L2 契约对账——删除行也算"存在"
# ══════════════════════════════════════════════

_CT = {"interfaces": ["DeviceService", "AlarmSender"]}


def _diff(sign):
    return ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -1,2 +1,2 @@\n"
            f"{sign}class DeviceService {{}}\n{sign}class AlarmSender {{}}\n")


def test_symbols_only_in_deleted_lines_are_missing():
    """★实测 5/5 契约符号全在 `-` 行，闸门仍判 True（26 号文 I-H3）★
    符号只在删除行出现，语义恰恰是"它被移除了"，与"契约已实现"完全相反。"""
    ok, issues = check_contract_in_diff(_diff("-"), _CT)
    assert ok is False and issues


def test_symbols_in_added_lines_pass():
    ok, _ = check_contract_in_diff(_diff("+"), _CT)
    assert ok is True


def test_symbols_in_context_lines_pass():
    """★上下文行保留为有效证据★：符号已在基线、本次只改其内部实现是合法形态，
    一刀切只认新增行会把正常的 modify 全部误判缺失。"""
    ok, _ = check_contract_in_diff(_diff(" "), _CT)
    assert ok is True


def test_filename_in_diff_header_is_not_evidence():
    """diff 元信息里的词不算实现证据——否则改个同名文件就能"证明"契约已实现。"""
    d = ("diff --git a/DeviceService.java b/DeviceService.java\n"
         "--- a/DeviceService.java\n+++ b/DeviceService.java\n@@ -1 +1 @@\n"
         "+// 只加了一行注释\n")
    ok, issues = check_contract_in_diff(d, {"interfaces": ["DeviceService"]})
    assert ok is False and issues


def test_non_discriminating_symbols_are_reported_not_dropped(caplog):
    """★泛词必须【如实报告】而不是剔除（26 号文 I-H3 第一重）★
    `contract_symbols` 对 API 条目取路径末段：`GET /system/device/list` → `list`，
    这种泛词在任何 diff 里都必然命中，等于该契约条目根本没被验证。
    剔除它会缩小分母把通过率做得更好看——与诚实相反，故只告警。"""
    import logging
    ct = {"interfaces": ["DeviceService"], "apis": ["GET /system/device/list"]}
    d = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n@@ -0,0 +1,2 @@\n"
         "+class DeviceService {}\n+  List<X> list() {}\n")
    with caplog.at_level(logging.WARNING):
        ok, _ = check_contract_in_diff(d, ct)
    assert ok is True
    assert any("无判别力" in r.message for r in caplog.records)


def test_shared_symbol_extractor_is_untouched():
    """★共享单一事实源不动，消费契约随后果分档★
    `contract_symbols` 同时是 C1 规划期对账的事实源，C1 的消费契约（做符号 owner 归属）
    里泛词是可用的。改共享表会重演本轮 W1 复核 HIGH-2 的错误
    （见记忆 swarm-reuse-contract-not-just-source）。"""
    from swarm.brain.contract_utils import contract_symbols
    assert contract_symbols({"apis": ["GET /system/device/list"]}) == ["list"]
