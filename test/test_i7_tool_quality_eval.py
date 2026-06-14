"""I7 工具集质量 eval（Anthropic writing-tools 准则）。

不是运行时逻辑，而是质量门禁：校验 Worker 12 个工具满足"高质量工具"标准——
有清晰 description、参数有类型、命名无歧义、职责边界清晰（无 Anthropic 所说的
"overlapping functionality + ambiguous decision points"）。

Anthropic 准则要点：
- 工具应自包含、对 agent 清晰、参数描述无歧义
- "若人类工程师都说不清何时该用哪个工具，AI 更不行"——故校验关键易混工具有区分性描述
- 少而精：避免职责重叠的冗余工具

未来加工具/改描述时此 eval 兜底，防质量退化。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.worker.agent import _get_worker_tools


def _tools():
    return {t.name: t for t in _get_worker_tools()}


def test_all_tools_have_description():
    """每个工具都有非空、足够信息量的 description（Anthropic：工具对 agent 必须清晰）。"""
    for name, t in _tools().items():
        desc = (t.description or "").strip()
        assert desc, f"工具 {name} 缺 description"
        assert len(desc) >= 10, f"工具 {name} description 过短（信息量不足）: {desc!r}"
    print("  ✅ 所有工具有充分 description")


def test_tools_have_documented_params():
    """有参数的工具，其参数应在 schema 里有描述（agent 据此正确传参）。"""
    tools = _tools()
    # 关键多参数工具必须有参数 schema
    for name in ("read_file", "write_file", "patch_file", "search_in_file"):
        t = tools.get(name)
        assert t is not None, f"缺工具 {name}"
        schema = t.args_schema.model_json_schema() if t.args_schema else {}
        props = schema.get("properties", {})
        assert props, f"工具 {name} 无参数 schema"
    print("  ✅ 关键工具有参数 schema")


def test_no_ambiguous_write_overlap():
    """write_file vs patch_file 是 Anthropic 点名的易混对：必须有区分性描述
    （全量覆盖 vs 精确编辑），否则 agent 选不对。"""
    tools = _tools()
    wf = (tools["write_file"].description or "")
    pf = (tools["patch_file"].description or "")
    # write_file 强调"覆盖/完整内容"，patch_file 强调"精确/替换片段"
    assert ("覆盖" in wf or "完整" in wf or "overwrite" in wf.lower()), \
        f"write_file 描述未体现'覆盖全量'语义，易与 patch 混淆: {wf!r}"
    assert ("替换" in pf or "精确" in pf or "patch" in pf.lower() or "old_string" in pf), \
        f"patch_file 描述未体现'精确替换'语义: {pf!r}"
    print("  ✅ write_file/patch_file 职责区分清晰")


def test_tool_set_is_lean():
    """工具集精简（Anthropic：few thoughtful tools）。12 个左右，不臃肿。
    名称唯一、无重复注册。"""
    tools = _get_worker_tools()
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), f"工具名重复: {names}"
    assert 8 <= len(names) <= 16, f"工具数 {len(names)} 异常（预期 8-16，精简且够用）"
    # 覆盖核心能力域
    joined = " ".join(names)
    for cap in ("read", "write", "patch", "git", "compile", "test", "knowledge"):
        assert cap in joined, f"工具集缺核心能力: {cap}"
    print(f"  ✅ 工具集精简（{len(names)} 个，覆盖核心能力域）")


def test_command_tools_distinguishable():
    """run_command vs run_compile/run_tests：run_command 是通用逃生舱，
    run_compile/run_tests 是语言感知封装——描述应体现各自定位（避免 agent 乱用 run_command）。"""
    tools = _tools()
    rc = (tools["run_command"].description or "")
    rcomp = (tools["run_compile"].description or "")
    rtest = (tools["run_tests"].description or "")
    assert rc and rcomp and rtest
    # run_compile/run_tests 应体现"编译"/"测试"专用语义
    assert "编译" in rcomp or "compile" in rcomp.lower()
    assert "测试" in rtest or "test" in rtest.lower()
    print("  ✅ 命令类工具定位可区分")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v", "-s"]))
