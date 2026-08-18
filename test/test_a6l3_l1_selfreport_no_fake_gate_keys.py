"""32 号文 A6-L3：`_parse_l1_result` 不得产出 `compile_passed` / `tests_passed`。

**病根**：这两个键由**正则匹配 LLM 自报文本**派生（`编译.*通过` / `tests?.*pass`），
写在两处、**全仓零生产读点**。架构上它们本就不该有消费者——本函数解析的是 LLM 自报＝
**弱信号**，而"编译过没过/测试过没过"的权威是 `_deterministic_l1_gate`
（`_parse_l1_result` 的 docstring 第一段就写着这件事）。

**为什么治法是删而非补消费者**（findings 同结论）：留着的真危害是
**下一个维护者会把它当权威读**——两个名字长得就像确定性闸结论，而值来自 LLM 措辞匹配。
一句"编译时通过了语法检查，但测试出现 error 失败"就能让 `compile_passed=True`，
而真实编译可能根本没跑。

★这不是"结构焊死测试"★ 断的是**函数的返回契约**（输出里有哪些键）＝行为，
不是实现细节；且单一事实源就是这个 dict 本身。判据：把两个键加回去，本文件必红。
"""
from __future__ import annotations


def _ex():
    """取一个能调 `_parse_l1_result` 的最小对象（该方法不依赖实例状态）。"""
    from swarm.worker.executor_l1gate import _L1GateMixin

    class _Stub(_L1GateMixin):
        pass

    return _Stub()


_FAKE_GATE_KEYS = ("compile_passed", "tests_passed")


def test_normal_path_has_no_fake_gate_keys():
    """★核心锁★ 正常解析路径不得产出这两个伪闸键。

    夹具刻意用**会让旧正则命中**的文本（"编译通过" + "测试全部通过"）——若键被加回来，
    它们会是 True，这条必红。用不命中的文本测等于没测（键在但为 False 也是缺陷）。
    """
    _, details = _ex()._parse_l1_result("编译通过，测试全部通过 ✅")
    for _k in _FAKE_GATE_KEYS:
        assert _k not in details, (
            f"★伪闸键 {_k} 又出现了★ 它由正则匹配 LLM 自报文本派生，与确定性闸结论无关；"
            f"名字却长得像权威结论 ⇒ 会被下一个维护者当真读。实得键={sorted(details)}"
        )


def test_unavailable_path_has_no_fake_gate_keys():
    """模型拒答/截断路径同样不得有（原实现两处都写，删一处＝半落地）。"""
    # ★夹具必须用真实拒答标记★ 第一版我写 "I cannot help with that request."——
    # 它**不在** `_REFUSAL_MARKERS` 里（那张表收的是 "i cannot complete" /
    # "unable to complete" 等特异措辞）⇒ 走的是正常路径，这条锁**空过**。
    # 是下面那句夹具前提断言当场逮到的；口径取自 `worker/l1_verdict._REFUSAL_MARKERS`。
    _, details = _ex()._parse_l1_result("I cannot complete this task without more steps.")
    assert details.get("llm_self_report") == "unavailable", (
        f"夹具前提：这段文本必须被判成拒答/截断，否则本条测的不是那条臂。实得 {details}"
    )
    for _k in _FAKE_GATE_KEYS:
        assert _k not in details, f"拒答臂仍有伪闸键 {_k}：{sorted(details)}"


def test_self_report_contract_still_intact():
    """正向契约：该函数仍须给出**自报**信号（别把该留的一起删了）。

    ★配对锁★ 只有上面两条时，把整个 details 改成 `{}` 也能全绿——那会让
    `llm_self_report` 的既有消费者拿不到值。这条钉住"删的是伪闸键，不是自报本身"。
    """
    passed, details = _ex()._parse_l1_result("L1_RESULT: PASS")
    assert passed is True
    assert details["llm_self_report"] == "pass", details
    assert "raw_result" in details, f"raw_result 是既有诊断面，不得删：{sorted(details)}"

    passed2, details2 = _ex()._parse_l1_result("L1_RESULT: FAIL")
    assert passed2 is False
    assert details2["llm_self_report"] == "fail", details2


def test_deterministic_gate_remains_the_authority_for_compile_verdict():
    """区分力：自报文本说"编译通过"不得让任何返回值声称编译过了。

    ★这条是本条 finding 的语义核心★ 治前 `compile_passed` 会是 True，而真实编译
    可能根本没跑（自报是 LLM 写的散文）。现在返回值里**没有任何键**声称编译结论——
    要拿编译结论只能去问确定性闸。
    """
    _, details = _ex()._parse_l1_result(
        "编译时通过了语法检查，但测试出现 error 失败")
    # 自报侧结论仍如实（有失败信号 ⇒ fail）
    assert details["llm_self_report"] == "fail", details
    # 且不得有任何键把"编译通过"这个措辞抬成结论
    _claims = [k for k, v in details.items()
               if k not in ("raw_result", "raw_refusal") and v is True]
    assert not _claims, (
        f"★返回值里出现了 True 的结论性键 {_claims}★ 本函数只解析弱自报，"
        f"任何 True 都会被读成'某件事验过了'。实得 {details}"
    )
