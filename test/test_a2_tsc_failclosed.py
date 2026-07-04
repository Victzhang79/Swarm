"""A2 治本（round22 假绿门）：tsc 非 "error TS" 失败漏网。

根因：_compile_files 的 tsc 分支 `if infra→skip; elif "error TS" in out→False`，
其余 rc!=0（非 infra 且输出不含字面 "error TS"：解析错误/声明错误/中文本地化输出/
自定义报错）落到函数末尾 `return True, "compile ok"` → 真实 TS 失败被当编译通过（静默假绿）。

治本：fail-closed —— rc!=0 且非 infra → return False（不依赖字面子串）。
基础设施瞬时失败仍 skip（无网装 typescript/tsc 缺失，非能力失败）。

行为测试：mock _run_check_split / _manifest_present / _is_infra_failure，不焊死结构。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.worker import l1_pipeline


def _compile_ts():
    return l1_pipeline._compile_files("/tmp/swarm-a2", ["src/app.ts"])


def test_tsc_noninfra_without_error_TS_substring_fails():
    """rc!=0、非 infra、输出【不含】"error TS" → 必须 fail-closed 判 False（核心复现）。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=True), \
         patch.object(l1_pipeline, "_is_infra_failure", return_value=False), \
         patch.object(l1_pipeline, "_run_check_split",
                      return_value=(2, "src/app.ts(3,1): 语法解析失败：意外的标记", "")):
        ok, detail = _compile_ts()
    assert ok is False, f"非 infra 的 tsc 失败必须判 False，got {ok} {detail!r}"


def test_tsc_error_TS_still_fails():
    """含字面 "error TS" 的失败仍判 False（回归保护）。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=True), \
         patch.object(l1_pipeline, "_is_infra_failure", return_value=False), \
         patch.object(l1_pipeline, "_run_check_split",
                      return_value=(1, "src/app.ts(3,1): error TS2304: Cannot find name 'foo'.", "")):
        ok, detail = _compile_ts()
    assert ok is False, detail


def test_tsc_infra_failure_skipped_ok():
    """基础设施瞬时失败（tsc 缺失/无网）→ 跳过编译闸门，不误判为能力失败。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=True), \
         patch.object(l1_pipeline, "_is_infra_failure", return_value=True), \
         patch.object(l1_pipeline, "_run_check_split",
                      return_value=(127, "npx: command not found", "")):
        ok, detail = _compile_ts()
    assert ok is True, f"infra 失败应 skip 判过，got {ok} {detail!r}"


def test_tsc_clean_pass_ok():
    """rc=0 干净通过 → True。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=True), \
         patch.object(l1_pipeline, "_is_infra_failure", return_value=False), \
         patch.object(l1_pipeline, "_run_check_split", return_value=(0, "", "")):
        ok, detail = _compile_ts()
    assert ok is True, detail


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A2 tsc fail-closed: {len(fns)}/{len(fns)} passed ===")
