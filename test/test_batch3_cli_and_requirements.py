"""第三批收口：CLI 三态与短密需求分母的接线回归。"""

from __future__ import annotations

import asyncio
import json

import pytest
from click.testing import CliRunner

from swarm import cli
from swarm.brain import nodes
from swarm.brain.requirements_extract import (
    MAX_EXTRACT_RETRIES,
    _minimum_expected_items,
    deterministic_evidence_coverage,
    extract_requirements,
    validate_requirement_items,
)


class _DenseRequirementLLM:
    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return type("Response", (), {"content": json.dumps(self.payload)})()


def test_short_dense_prd_low_yield_marks_denominator_incomplete(monkeypatch):
    """短 PRD 的五个明确义务只抽一条时，字符数启发式不能冒充完整性证明。"""
    source = "\n".join([
        "1. 系统必须支持批量导入。",
        "2. 系统必须逐行报告导入错误。",
        "3. 系统必须记录审计日志。",
        "4. 系统必须允许管理员回滚。",
        "5. 系统必须限制上传文件大小。",
    ])
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持批量导入",
        "kind": "functional",
        "source_quote": "系统必须支持批量导入",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert len(out["requirement_items"]) == 1
    assert out["requirement_denominator_complete"] is False
    assert out["requirement_denominator_reason"] == "evidence_units_uncovered"
    assert "requirements_extract:evidence_coverage=1<5" in out["degraded_reasons"]
    assert "requirements_extract:insufficient_count=1<5" in out["degraded_reasons"]


def test_numbered_requirement_list_is_a_structural_denominator(monkeypatch):
    """明确编号枚举本身就是确定性分母证据，不要求作者重复写“必须”。"""
    source = "\n".join([
        "1. 支持批量导入数据。",
        "2. 逐行报告导入错误。",
        "3. 记录管理员审计日志。",
        "4. 允许管理员回滚任务。",
        "5. 限制上传文件大小。",
    ])
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持批量导入",
        "kind": "functional",
        "source_quote": "支持批量导入数据",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert out["requirement_denominator_complete"] is False
    assert "requirements_extract:insufficient_count=1<5" in out["degraded_reasons"]


def test_markdown_table_data_rows_are_a_structural_denominator(monkeypatch):
    """带标准分隔行的功能表应按数据行计分母，不把表头和分隔符算成需求。"""
    source = """| 功能 | 说明 |
| --- | --- |
| 批量导入 | 导入数据文件 |
| 错误报告 | 逐行展示原因 |
| 审计日志 | 记录管理员操作 |
| 任务回滚 | 恢复上一次状态 |
| 上传限制 | 拒绝超大文件 |
"""
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持批量导入",
        "kind": "functional",
        "source_quote": "批量导入 | 导入数据文件",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert out["requirement_denominator_complete"] is False
    assert "requirements_extract:insufficient_count=1<5" in out["degraded_reasons"]


def test_markdown_table_rows_with_empty_cells_still_count(monkeypatch):
    source = """| 功能 | 备注 |
| --- | --- |
| 批量导入 | |
| 错误报告 | |
| 审计日志 | |
| 任务回滚 | |
| 上传限制 | |
"""
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持批量导入",
        "kind": "functional",
        "source_quote": "批量导入",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert out["requirement_denominator_complete"] is False
    assert "requirements_extract:insufficient_count=1<5" in out["degraded_reasons"]


def test_non_requirement_role_table_does_not_inflate_denominator(monkeypatch):
    source = """| 角色 | 说明 |
| --- | --- |
| 运维 | 负责值班 |
| 开发 | 接入系统 |
| 管理员 | 管理权限 |
| 运营 | 查看报表 |
| 审计员 | 检查记录 |

系统必须支持登录。
"""
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持登录",
        "kind": "functional",
        "source_quote": "系统必须支持登录",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1
    assert out["requirement_denominator_complete"] is True


@pytest.mark.parametrize(
    "inventory",
    [
        "字段 | 类型\n--- | ---\n用户名 | string\n密码 | string\n状态 | integer",
        "接口 | 状态\n--- | ---\n/login | 200\n/logout | 204\n/me | 200",
    ],
)
def test_ambiguous_inventory_tables_do_not_inflate_denominator(monkeypatch, inventory):
    """数据字典/API 盘点不是逐行功能需求；保守分母不能改变抽取粒度。"""
    source = inventory + "\n\n系统必须支持登录。"
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持登录",
        "kind": "functional",
        "source_quote": "系统必须支持登录",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1
    assert out["requirement_denominator_complete"] is True


def test_existing_feature_status_table_does_not_inflate_denominator(monkeypatch):
    source = """现有功能 | 状态
--- | ---
登录 | 已上线
导出 | 已上线
搜索 | 已上线

本次必须支持支付。
"""
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持支付", "kind": "functional", "source_quote": "本次必须支持支付",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1
    assert out["requirement_denominator_complete"] is True


@pytest.mark.parametrize(
    "header", ["需求 | 状态", "功能 | 状态", "需求清单 | 负责人", "当前需求 | 验收"],
)
def test_requirement_status_table_counts_without_explicit_inventory_context(header):
    source = header + "\n--- | ---\n导入 | 待实现\n导出 | 待实现\n登录 | 待实现"
    assert _minimum_expected_items(source) == 3


@pytest.mark.parametrize(
    "heading",
    [
        "在现有技术栈基础上，本次需求如下：",
        "团队确认后的需求：",
        "环境适配要求：",
    ],
)
def test_requirement_heading_overrides_metadata_words(monkeypatch, heading):
    source = heading + "\n- 支持导入\n- 报告错误\n- 记录审计"
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持导入", "kind": "functional", "source_quote": "支持导入",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert "requirements_extract:insufficient_count=1<3" in out["degraded_reasons"]


def test_deterministic_requirement_structures_are_not_capped_at_twenty():
    marker_source = "；".join(f"系统必须实现功能{i}" for i in range(30))
    table_source = "功能 | 说明\n--- | ---\n" + "\n".join(
        f"功能{i} | 说明{i}" for i in range(30)
    )

    assert _minimum_expected_items(marker_source) == 30
    assert _minimum_expected_items(table_source) == 30


def test_mixed_requirement_list_and_table_sum_non_overlapping_evidence():
    source = """需求清单：
- 支持批量导入
- 逐行报告错误

需求 | 验收
--- | ---
审计日志 | 记录管理员操作
任务回滚 | 恢复上一次状态
"""

    assert _minimum_expected_items(source) == 4


def test_same_source_quote_cannot_pad_requirement_denominator():
    source = "系统必须支持导入、报告错误并记录审计日志。"
    quote = "必须支持导入、报告错误并记录审计日志"
    items, rejected = validate_requirement_items([
        {"text": "支持导入", "source_quote": quote},
        {"text": "报告错误", "source_quote": quote},
        {"text": "记录审计日志", "source_quote": quote},
    ], source)

    assert len(items) == 1
    assert [row["reason"] for row in rejected] == ["duplicate_quote", "duplicate_quote"]


def test_overlapping_quote_variants_cannot_reuse_one_source_obligation():
    source = "系统必须支持登录。系统必须支持导出。系统必须记录审计。"
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": "系统必须支持登录"},
        {"text": "支持导出", "source_quote": "必须支持登录"},
        {"text": "记录审计", "source_quote": "支持登录"},
    ], source)

    assert len(items) == 1
    assert [row["reason"] for row in rejected] == ["duplicate_quote", "duplicate_quote"]


def test_quote_spanning_two_explicit_obligations_is_ambiguous():
    source = "系统必须支持登录并且必须记录审计。"
    quote = "系统必须支持登录并且必须记录审计"
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": quote},
        {"text": "记录审计", "source_quote": quote},
    ], source)

    assert items == []
    assert [row["reason"] for row in rejected] == ["quote_ambiguous", "quote_ambiguous"]


def test_specific_quotes_cover_two_explicit_obligations_independently():
    source = "系统必须支持登录并且必须记录审计。"
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": "系统必须支持登录并且"},
        {"text": "记录审计", "source_quote": "必须记录审计"},
    ], source)

    assert len(items) == 2
    assert rejected == []


def test_tier2_table_quote_and_direct_variant_share_one_provenance_span():
    source = """需求 | 说明
--- | ---
登录 | 必须支持SSO
导出 | 必须支持PDF
审计 | 必须记录日志
"""
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": "登录 必须支持SSO"},
        {"text": "支持导出", "source_quote": "必须支持SSO"},
        {"text": "记录审计", "source_quote": "登录 支持SSO"},
    ], source)

    assert len(items) == 1
    assert [row["reason"] for row in rejected] == ["duplicate_quote", "duplicate_quote"]


def test_disjoint_quote_fragments_from_one_table_row_share_one_source_unit():
    source = """需求 | 说明
--- | ---
用户登录 | 必须支持企业级单点认证
数据导出 | 必须支持PDF
操作审计 | 必须记录日志
"""
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": "用户登录"},
        {"text": "支持导出", "source_quote": "必须支持"},
        {"text": "记录审计", "source_quote": "企业级单点认证"},
    ], source)

    assert len(items) == 1
    assert [row["reason"] for row in rejected] == ["quote_ambiguous", "duplicate_quote"]


@pytest.mark.parametrize(
    "detail",
    [r"支持 CSV \| XLSX", "支持 `CSV | XLSX`"],
)
def test_table_cell_literal_pipes_do_not_break_requirement_rows(detail):
    source = (
        "功能 | 说明\n--- | ---\n"
        f"导入 | {detail}\n导出 | PDF\n登录 | SSO"
    )
    assert _minimum_expected_items(source) == 3


def test_borderless_table_with_empty_last_cells_counts_rows():
    source = "功能 | 备注\n--- | ---\n批量导入 |\n错误报告 |\n审计日志 |"
    assert _minimum_expected_items(source) == 3


@pytest.mark.parametrize(
    "source",
    [
        """| 需求 | 说明 | 负责人 |
| --- | --- | --- |
| 登录 | 支持登录 |
| 导出 | 支持导出 |
| 审计 | 记录日志 |""",
        """需求 | 说明 | 负责人
--- | --- | ---
登录 | 支持登录
导出 | 支持导出
审计 | 记录日志""",
        """需求 | 说明
--- | ---
登录 | 支持登录 | P0
导出 | 支持导出 | P1
审计 | 记录日志 | P2""",
    ],
)
def test_requirement_table_rows_may_omit_trailing_cells(source):
    assert _minimum_expected_items(source) == 3


@pytest.mark.parametrize(
    "heading",
    [
        "## 现有功能盘点",
        "当前能力清单：",
        "Current inventory:",
        "Existing features:",
        "Current features:",
    ],
)
def test_inventory_heading_excludes_generic_feature_table(heading):
    source = heading + "\n\n功能 | 状态\n--- | ---\n登录 | 已上线\n导出 | 已上线\n搜索 | 已上线"
    assert _minimum_expected_items(source) == 1


@pytest.mark.parametrize(
    "heading",
    ["Current inventory:", "Existing features:", "Current features:"],
)
def test_english_inventory_heading_excludes_generic_list(heading):
    assert _minimum_expected_items(heading + "\n- Login\n- Export\n- Audit") == 1


@pytest.mark.parametrize(
    "heading",
    ["本次需求：", "当前需求：", "This task requirements:", "Current requirements:"],
)
def test_requirement_heading_includes_generic_feature_table(heading):
    source = heading + "\n\n功能 | 状态\n--- | ---\n登录 | 待实现\n导出 | 待实现\n搜索 | 待实现"
    assert _minimum_expected_items(source) == 3


def test_multiple_explicit_obligations_on_one_line_count_separately(monkeypatch):
    source = "系统必须支持导入，并且必须报告错误，而且必须记录审计日志。"
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持导入",
        "kind": "functional",
        "source_quote": "系统必须支持导入",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert "requirements_extract:insufficient_count=1<3" in out["degraded_reasons"]


def test_count_padding_with_table_header_cannot_fake_evidence_coverage(monkeypatch):
    source = """需求 | 说明
--- | ---
登录 | 必须支持SSO
导出 | 必须支持PDF
审计 | 必须记录日志
"""
    llm = _DenseRequirementLLM({"items": [
        {"text": "支持登录", "source_quote": "登录 | 必须支持SSO"},
        {"text": "支持导出", "source_quote": "导出 | 必须支持PDF"},
        {"text": "补充说明", "source_quote": "需求 | 说明"},
    ]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert out["requirement_denominator_complete"] is False
    assert out["requirement_denominator_reason"] == "evidence_units_uncovered"
    assert "requirements_extract:evidence_coverage=2<3" in out["degraded_reasons"]


@pytest.mark.parametrize(
    "source, padding_quote",
    [
        (
            "需求 | 说明\n--- | ---\n"
            "登录 | 支持SSO;兼容LDAP\n"
            "导出 | 支持PDF；保留格式\n"
            "审计 | 记录日志。可追溯\n",
            "需求 | 说明",
        ),
        (
            "需求清单：\n"
            "- 支持SSO;兼容LDAP\n"
            "- 支持PDF；保留格式\n"
            "- 记录日志。可追溯\n",
            "需求清单",
        ),
    ],
)
def test_structured_rows_keep_evidence_identity_with_inner_punctuation(
    monkeypatch, source, padding_quote,
):
    llm = _DenseRequirementLLM({"items": [
        {"text": "支持登录", "source_quote": "支持SSO"},
        {"text": "支持导出", "source_quote": "支持PDF"},
        {"text": "补充说明", "source_quote": padding_quote},
    ]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert out["requirement_denominator_complete"] is False
    assert "requirements_extract:evidence_coverage=2<3" in out["degraded_reasons"]


def test_base_and_extended_rows_remain_distinct_evidence(monkeypatch):
    source = """Requirements:
- System must support login
- System must support login with MFA
- System must export data
"""
    items, rejected = validate_requirement_items([
        {"text": "Support login", "source_quote": "System must support login"},
        {"text": "Support MFA login", "source_quote": "System must support login with MFA"},
        {"text": "Export data", "source_quote": "System must export data"},
    ], source)

    assert _minimum_expected_items(source) == 3
    assert len(items) == 3
    assert rejected == []

    llm = _DenseRequirementLLM({"items": [
        {"text": "Support MFA login", "source_quote": "System must support login with MFA"},
        {"text": "Export data", "source_quote": "System must export data"},
    ]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert out["requirement_denominator_complete"] is False
    assert "requirements_extract:evidence_coverage=2<3" in out["degraded_reasons"]


def test_same_requirement_in_prose_and_table_is_one_evidence_slot():
    source = """系统必须支持登录。

需求 | 说明
--- | ---
登录 | 系统必须支持登录
"""
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": "系统必须支持登录"},
    ], source)

    assert _minimum_expected_items(source) == 1
    assert len(items) == 1
    assert rejected == []
    assert deterministic_evidence_coverage(items, source) == (1, 1)


def test_pipe_prose_after_table_is_not_reclassified_as_table_evidence():
    source = """需求 | 说明
--- | ---
登录 | 支持SSO
导出 | 支持PDF

登录 | 支持SSO 的现有实现说明，不是新需求；仅供背景参考
"""
    items, rejected = validate_requirement_items([
        {"text": "支持登录", "source_quote": "登录 | 支持SSO"},
        {"text": "支持导出", "source_quote": "导出 | 支持PDF"},
    ], source)

    assert rejected == []
    assert _minimum_expected_items(source) == 2
    assert deterministic_evidence_coverage(items, source) == (2, 2)


@pytest.mark.parametrize(
    "source",
    [
        "系统需要支持登录。系统需要支持导出。系统需要记录审计。",
        "系统应该支持登录。系统应该支持导出。系统应该记录审计。",
        "系统需支持登录。系统需支持导出。系统需记录审计。",
        "系统须支持登录。系统须支持导出。系统须记录审计。",
        "The system should support login. The system should export data. The system should audit changes.",
    ],
)
def test_common_explicit_obligation_markers_count_each_clause(source):
    assert _minimum_expected_items(source) == 3


def test_non_obligation_need_fragments_do_not_inflate_denominator():
    source = "供需分析可以展示。数据按需加载。访客无需登录。系统可以支持导出。"
    assert _minimum_expected_items(source) == 1


@pytest.mark.parametrize(
    "rows",
    [
        "(1) 支持登录\n(2) 支持导出\n(3) 记录审计",
        "一、支持登录\n二、支持导出\n三、记录审计",
        "① 支持登录\n② 支持导出\n③ 记录审计",
        "1．支持登录\n2．支持导出\n3．记录审计",
    ],
)
def test_common_document_numbering_counts_requirement_rows(rows):
    assert _minimum_expected_items("需求：\n" + rows) == 3


@pytest.mark.parametrize(
    "rows",
    [
        "(a) Support login\n(b) Support export\n(c) Record audit\n(d) Allow rollback",
        "Requirement 1: Support login\nRequirement 2: Support export\n"
        "Requirement 3: Record audit\nRequirement 4: Allow rollback",
    ],
)
def test_lettered_and_requirement_prefixed_numbering_count_rows(rows):
    assert _minimum_expected_items("Requirements:\n" + rows) == 4


def test_lettered_metadata_list_is_still_excluded():
    rows = "(a) Python\n(b) PostgreSQL\n(c) Redis"
    assert _minimum_expected_items("Technology stack:\n" + rows) == 1


@pytest.mark.parametrize(
    "rows",
    [
        "(1) Python\n(2) PostgreSQL\n(3) Redis",
        "一、开发\n二、测试\n三、运维",
        "① v1\n② v2\n③ v3",
        "1．Linux\n2．macOS\n3．Windows",
    ],
)
def test_common_document_numbering_still_excludes_metadata_lists(rows):
    assert _minimum_expected_items("技术栈与团队版本：\n" + rows) == 1


@pytest.mark.parametrize("bullet", ["•", "·", "–"])
def test_common_document_bullets_count_requirement_rows(bullet):
    rows = f"{bullet} 支持登录\n{bullet} 支持导出\n{bullet} 记录审计"
    assert _minimum_expected_items("需求：\n" + rows) == 3


@pytest.mark.parametrize("bullet", ["•", "·", "–"])
def test_common_document_bullets_still_exclude_metadata_lists(bullet):
    rows = f"{bullet} Python\n{bullet} PostgreSQL\n{bullet} Redis"
    assert _minimum_expected_items("技术栈：\n" + rows) == 1


@pytest.mark.parametrize(
    "heading, rows",
    [
        ("现有功能清单：", "- 登录\n- 导出\n- 搜索"),
        ("当前能力盘点：", "• 登录\n• 导出\n• 搜索"),
        ("现有模块清单：", "一、登录\n二、导出\n三、搜索"),
    ],
)
def test_inventory_context_excludes_generic_structured_lists(heading, rows):
    assert _minimum_expected_items(heading + "\n" + rows) == 1


@pytest.mark.parametrize(
    "heading",
    [
        "本次功能需求：",
        "本次功能清单：",
        "当前需求：",
        "现有功能需要改进：",
        "当前能力应该调整：",
    ],
)
def test_strong_requirement_context_overrides_inventory_words(heading):
    assert _minimum_expected_items(heading + "\n- 登录\n- 导出\n- 搜索") == 3


def test_outer_table_body_with_only_first_cell_still_counts_rows():
    source = """| 需求 | 说明 | 负责人 |
| --- | --- | --- |
| 登录 |
| 导出 |
| 审计 |"""
    assert _minimum_expected_items(source) == 3


def test_current_scope_overrides_inventory_word_for_feature_table():
    source = "本次功能清单：\n\n功能 | 状态\n--- | ---\n登录 | 待实现\n导出 | 待实现\n审计 | 待实现"
    assert _minimum_expected_items(source) == 3


@pytest.mark.parametrize(
    "source",
    [
        "系统必须支持用户名登录。\n\n需求 | 说明\n--- | ---\n登录 | 支持用户名登录",
        "需求：\n- 支持用户名登录\n\n需求 | 说明\n--- | ---\n登录 | 支持用户名登录",
    ],
)
def test_repeated_summary_and_table_evidence_do_not_double_count(source):
    assert _minimum_expected_items(source) == 1


def test_distinct_list_and_table_requirements_still_sum():
    source = """需求：
- 支持批量导入
- 支持批量导出

需求 | 说明
--- | ---
登录 | 支持用户名登录
审计 | 记录管理员操作
"""
    assert _minimum_expected_items(source) == 4


def test_outer_table_row_markers_are_not_counted_again_as_table_row():
    source = """| 需求 | 说明 |
| --- | --- |
| 登录 | 必须支持SSO且必须记录审计 |
"""
    assert _minimum_expected_items(source) == 2


def test_markdown_table_without_outer_pipes_still_counts(monkeypatch):
    source = """功能 | 说明
--- | ---
批量导入 | 导入文件
错误报告 | 逐行展示
审计日志 | 记录操作
任务回滚 | 恢复状态
上传限制 | 拒绝超大文件
"""
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持批量导入",
        "kind": "functional",
        "source_quote": "批量导入 | 导入文件",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1 + MAX_EXTRACT_RETRIES
    assert "requirements_extract:insufficient_count=1<5" in out["degraded_reasons"]


@pytest.mark.parametrize(
    "metadata",
    [
        "技术栈：\n- Python\n- PostgreSQL\n- Redis",
        "团队成员：\n1. 张三\n2. 李四\n3. 王五",
    ],
)
def test_metadata_lists_do_not_inflate_requirement_denominator(monkeypatch, metadata):
    source = metadata + "\n\n系统必须支持登录。"
    llm = _DenseRequirementLLM({"items": [{
        "text": "支持登录",
        "kind": "functional",
        "source_quote": "系统必须支持登录",
    }]})
    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: llm)

    out = asyncio.run(extract_requirements({"task_description": source}))

    assert llm.calls == 1
    assert out["requirement_denominator_complete"] is True


def test_cli_submit_omits_auto_accept_when_flag_is_not_given(monkeypatch):
    """CLI 未给开关时保留 None，让 API/环境默认成为唯一解析权威。"""
    captured: dict = {}

    async def _capture(description, project, watch, auto_accept, api_url):
        captured.update({
            "description": description,
            "project": project,
            "watch": watch,
            "auto_accept": auto_accept,
            "api_url": api_url,
        })

    monkeypatch.setattr(cli, "_submit_via_api", _capture)

    result = CliRunner().invoke(cli.main, ["submit", "修复问题", "-p", "project-1"])

    assert result.exit_code == 0, result.output
    assert captured["auto_accept"] is None


def test_cli_submit_sends_true_when_auto_accept_flag_is_given(monkeypatch):
    captured: dict = {}

    async def _capture(_description, _project, _watch, auto_accept, _api_url):
        captured["auto_accept"] = auto_accept

    monkeypatch.setattr(cli, "_submit_via_api", _capture)

    result = CliRunner().invoke(
        cli.main,
        ["submit", "修复问题", "-p", "project-1", "--auto-accept"],
    )

    assert result.exit_code == 0, result.output
    assert captured["auto_accept"] is True


def test_cli_submit_sends_false_when_auto_accept_is_explicitly_disabled(monkeypatch):
    captured: dict = {}

    async def _capture(_description, _project, _watch, auto_accept, _api_url):
        captured["auto_accept"] = auto_accept

    monkeypatch.setattr(cli, "_submit_via_api", _capture)

    result = CliRunner().invoke(
        cli.main,
        ["submit", "修复问题", "-p", "project-1", "--no-auto-accept"],
    )

    assert result.exit_code == 0, result.output
    assert captured["auto_accept"] is False
