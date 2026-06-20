"""回归：拒答/截断回复硬否决（Bug-4，task 0f93f1fc 实证）。

模型 "Sorry, need more steps" 等拒答/截断标记 = worker 没真正完成，产出不可信。
此前这类回复仅让 LLM 自报判 False，但 deterministic gate（diff非空+compile过）翻盘
判通过 → 幻觉 PASS。必须硬否决整个 L1，覆盖确定性闸门。
"""

from __future__ import annotations

from swarm.worker.executor import _is_refusal_or_truncated


def test_refusal_markers_detected():
    assert _is_refusal_or_truncated("Sorry, need more steps to process this request.")
    assert _is_refusal_or_truncated("I'm unable to complete this")
    assert _is_refusal_or_truncated("I am unable to do that")
    assert _is_refusal_or_truncated("Cannot complete this request right now")
    # 大小写不敏感
    assert _is_refusal_or_truncated("SORRY, NEED MORE STEPS")


def test_normal_output_not_flagged():
    assert not _is_refusal_or_truncated("L1_RESULT: PASS\n编译通过，测试通过")
    assert not _is_refusal_or_truncated("已新增 NumberUtils 工具类，包含 isNumeric 方法")
    assert not _is_refusal_or_truncated("SUMMARY: 修改完成 CONFIDENCE: high")


def test_empty_flagged_as_unavailable():
    """W1.2 commit②（契约刻意收紧）：空/纯空格/None 回复 = 模型截断/空转，
    无有效产出信号 → 按不可用处理(flagged)，非 PASS。此前空回复不 flagged，
    会被当"有内容"送进确定性闸门，compile 恰好过即幻觉 PASS。None 安全归一为空。"""
    assert _is_refusal_or_truncated("")
    assert _is_refusal_or_truncated("   ")
    assert _is_refusal_or_truncated(None)  # None 归一为空字符串，按不可用处理


def test_chinese_refusal_markers_detected():
    """W1.2 commit②：补齐中文拒答标记（本地中文模型常用中文措辞拒答）。"""
    assert _is_refusal_or_truncated("抱歉，我无法完成这个任务")
    assert _is_refusal_or_truncated("无法完成该请求")
    assert _is_refusal_or_truncated("需要更多步骤来处理")
    assert _is_refusal_or_truncated("这超出我的能力范围")


def test_substring_in_normal_text_edge():
    # "steps" 单独出现不应误判（必须命中完整短语）
    assert not _is_refusal_or_truncated("分为 3 个 steps 完成了任务")
    assert not _is_refusal_or_truncated("unable to reproduce 已在注释说明但已修复")  # 无 i'm/i am 前缀


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 拒答/截断硬否决: {len(fns)}/{len(fns)} passed ===")
