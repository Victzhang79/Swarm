"""主题G P1 · G2-1 归因面 + G2-2 工具观测面（用户三次点名"节点不清/工具零留痕"）。

G2-1：router [stream] 心跳/收尾/排队日志此前只记 model_name，看门狗一行分不清是
 plan_batch 还是 validate_plan 在长流/runaway。contextvar set_llm_node 挂在
 _invoke_llm_abortable._stream_once 入口、_astream_inner 日志点读取前缀。
G2-2：worker 沙箱 jsonl 只见 exec/shell，write_file/experience__<id> 等 LangGraph 工具
 零留痕→分不清"没挂"还是"挂了没用"。_record_tool_telemetry 从返回 messages 确定性
 归因调用/错误，累计进 l1_details.tool_telemetry（含 experience__ 前缀=技能 join 落库端）。
"""
from __future__ import annotations


# ══════════════ G2-1 router 节点归因 contextvar ══════════════

def test_g2_1_node_tag_set_and_reset():
    import swarm.models.router as rc
    assert rc._llm_node_tag() == "", "未绑定时无前缀"
    tok = rc.set_llm_node("plan_batch")
    assert rc._llm_node_tag() == "[plan_batch] ", "绑定后 [stream] 日志带节点前缀"
    rc.reset_llm_node(tok)
    assert rc._llm_node_tag() == "", "reset 精确还原（不泄漏到同任务后续 worker 流）"


def test_g2_1_empty_label_no_tag():
    import swarm.models.router as rc
    tok = rc.set_llm_node("")
    assert rc._llm_node_tag() == "", "空标签=无前缀（既有裸调用/桩路径零变化）"
    rc.reset_llm_node(tok)


def test_g2_1_reset_bad_token_never_raises():
    import swarm.models.router as rc
    # 跨任务/已重置的 token → reset 静默吞（观测面绝不抛拖垮调用）
    rc.reset_llm_node(object())
    rc.reset_llm_node(None)


# ══════════════ G2-2 worker 工具遥测 ══════════════

class _AIMsg:
    """模拟 AIMessage：带 tool_calls，无 type=='tool'（不触发错误归因分支）。"""
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _ToolMsg:
    """模拟 ToolMessage：type=='tool'，status 可为 'error'。"""
    type = "tool"

    def __init__(self, name, status="success"):
        self.name = name
        self.status = status


class _Dummy:
    """挂载 _record_tool_telemetry 的最小宿主（只需 subtask.id）。"""
    class _ST:
        id = "st-g2"
    subtask = _ST()


def _record(dummy, messages, step, monkeypatch=None, sink=None):
    from swarm.worker.executor_agent import _AgentLoopMixin
    if sink is not None:
        import swarm.worker.executor_agent as ea
        monkeypatch.setattr(ea.logger, "info", lambda *a, **k: sink.append(a))
    _AgentLoopMixin._record_tool_telemetry(dummy, messages, step)


def test_g2_2_counts_tool_calls_and_experience():
    d = _Dummy()
    msgs = [
        _AIMsg([{"name": "write_file"}, {"name": "run_command"}]),
        _AIMsg([{"name": "write_file"}, {"name": "experience__redis_lock"}]),
    ]
    _record(d, msgs, "coding")
    tel = d._tool_telemetry
    assert tel["calls"]["write_file"] == 2
    assert tel["calls"]["run_command"] == 1
    assert tel["calls"]["experience__redis_lock"] == 1, "经验工具计入=技能 join 落库端"


def test_g2_2_error_attribution():
    d = _Dummy()
    msgs = [_AIMsg([{"name": "run_command"}]),
            _ToolMsg("run_command", status="error"),
            _ToolMsg("write_file", status="success")]
    _record(d, msgs, "coding")
    assert d._tool_telemetry["errors"].get("run_command") == 1
    assert "write_file" not in d._tool_telemetry["errors"], "成功工具不计错误"


def test_g2_2_accumulates_across_steps():
    d = _Dummy()
    _record(d, [_AIMsg([{"name": "read_file"}])], "locating")
    _record(d, [_AIMsg([{"name": "read_file"}])], "coding")
    assert d._tool_telemetry["calls"]["read_file"] == 2, "跨 _run_agent 调用累计（per-subtask 总账）"


def test_g2_2_emits_structured_log(monkeypatch):
    d = _Dummy()
    sink: list = []
    _record(d, [_AIMsg([{"name": "write_file"}])], "coding", monkeypatch=monkeypatch, sink=sink)
    joined = " ".join(str(a) for a in sink)
    assert "tool-telemetry" in joined and "st-g2" in joined and "write_file" in joined, (
        "发一行结构化 [tool-telemetry]（grep 可判读，取代只发不 join 的 skills-telemetry）")


def test_g2_2_no_calls_no_log_no_sink(monkeypatch):
    d = _Dummy()
    sink: list = []
    _record(d, [_AIMsg([])], "locating", monkeypatch=monkeypatch, sink=sink)
    assert sink == [], "零工具调用不发噪声行"
    # tel 建立但 calls 空 → _make_output 不塞 l1_details（无遥测不污染）
    assert d._tool_telemetry["calls"] == {}


def test_g2_2_fail_open_on_bad_messages():
    d = _Dummy()
    # messages 非法（None 元素/怪对象）绝不抛——观测面 fail-open
    _record(d, [None, object()], "coding")


if __name__ == "__main__":
    print("run via pytest")
