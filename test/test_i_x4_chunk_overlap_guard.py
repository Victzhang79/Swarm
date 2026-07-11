"""主题I X-4（外部深审 MEDIUM）：chunk_overlap ≥ chunk_size → 切块 offset 步进 ≤0 死循环。

_flush_block 超长块按字符重叠切分：offset += chunk_size - chunk_overlap。overlap≥size 时
步进 ≤0 → offset 不进/倒退 → `while offset < len(text)` 永不终止（无进展死循环）。config
两字段独立上下界（size≥64, overlap≤1024）放行坏组合。
治：①semantic_index 步进 max(1,·) 保底（fail-safe，覆盖构造器直传绕过 API）；②config PUT
交叉校验 overlap<size（fail-loud）。
"""
from __future__ import annotations

from swarm.knowledge.semantic_index import _flush_block


def _run(chunk_size, chunk_overlap):
    # 超长文本（> chunk_size）逼入重叠切分分支；坏参数下旧实现在此死循环
    block = [("x" * 40) for _ in range(20)]  # 拼成 ~820 字符
    result: list = []
    _flush_block(block, "code", 1, 20, "f.py", None, None,
                 chunk_size, chunk_overlap, result)
    return result


def test_x4_overlap_gt_size_terminates():
    """overlap > size：必须终止（步进保底），且切出 chunk（退化但不挂死）。"""
    result = _run(chunk_size=64, chunk_overlap=1024)
    assert len(result) > 0, "坏参数下退化切块但绝不死循环/空结果"


def test_x4_overlap_eq_size_terminates():
    result = _run(chunk_size=100, chunk_overlap=100)
    assert len(result) > 0


def test_x4_normal_params_still_chunk():
    """正常参数（overlap < size）切块行为不变（无回归）。"""
    result = _run(chunk_size=100, chunk_overlap=20)
    assert len(result) >= 2, "820 字符按 step=80 应切多块"
    # config PUT 的交叉校验（overlap<size fail-loud）内联在端点里，需整 app+auth 才能测；
    # 此处覆盖真正会挂死的确定性 fail-safe（步进保底），端点校验作纵深防御不单独立测。


if __name__ == "__main__":
    print("run via pytest")
