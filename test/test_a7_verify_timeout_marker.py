"""A7（round22）：verify 阶段超时无 timeout_in_* marker → 上游认不出尺寸超时。

根因：coding/locating 超时写 timeout_in_coding/timeout_in_locating → _is_timeout_oversize_failure
识别 → 强制拆小（主干B 不变量：超时=工作单元太大，第一恢复动作应拆小非换模型重试大块）。
verify 循环超时旧仅 break、不写 marker → brain 走普通 retry 而非拆小（打地鼠的派发面对偶）。

治本：verify 超时写 l1_details["error"]="timeout_in_verifying" + 纳入 _TIMEOUT_OVERSIZE_MARKERS。

行为测试：直接验证 _is_timeout_oversize_failure 对新 marker 的识别 + preparing 仍排除。
"""
from __future__ import annotations

from swarm.brain.nodes import _is_timeout_oversize_failure
from swarm.types import WorkerOutput


def _out(err: str) -> WorkerOutput:
    return WorkerOutput(subtask_id="st-a7", status="failed", diff="", summary="",
                        l1_details={"error": err})


def test_verify_timeout_recognized_as_oversize():
    """timeout_in_verifying → 识别为尺寸超时（触发拆小）。"""
    assert _is_timeout_oversize_failure(_out("timeout_in_verifying")) is True


def test_coding_and_locating_still_oversize():
    """回归：既有 coding/locating marker 仍识别。"""
    assert _is_timeout_oversize_failure(_out("timeout_in_coding")) is True
    assert _is_timeout_oversize_failure(_out("timeout_in_locating")) is True


def test_preparing_timeout_not_oversize():
    """回归：preparing 超时是 infra 非尺寸问题，仍不算 oversize（不拆小）。"""
    assert _is_timeout_oversize_failure(_out("timeout_in_preparing")) is False


def test_non_timeout_error_not_oversize():
    assert _is_timeout_oversize_failure(_out("compile failed")) is False


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A7 verify 超时 marker: {len(fns)}/{len(fns)} passed ===")
