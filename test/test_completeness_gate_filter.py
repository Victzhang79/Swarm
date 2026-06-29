#!/usr/bin/env python3
"""P6b 完整性闸门精度治本：区分【缺功能子任务】vs【描述质量】。

第七轮 996：ELABORATE 文件拆分把子描述截成裸 stub → VALIDATE_PLAN LLM 如实标
"描述截断…缺少完整实现指引" → P6b 旧逻辑命中"缺少"关键词 → 误判缺核心功能 → 触发
徒劳全量重拆(ultra 项目成本极高、根因在拆分逻辑、重拆后仍会再截断)。
治本：带描述质量标记的 issue 一律放过，只有真缺功能/文件/表 DDL 才触发补齐。
"""

from swarm.brain.nodes import _filter_completeness_missing


def test_description_truncation_issue_does_not_trigger_replan():
    """"描述截断…缺少完整实现指引"是描述质量问题 → 不进补齐集合(不触发全量重拆)。"""
    issues = [
        "st-2-1 描述截断，缺少完整实现指引",
        "st-7-2 description appears truncated, missing implementation detail",
    ]
    assert _filter_completeness_missing(issues) == []


def test_real_missing_feature_still_triggers():
    """真缺功能/缺表 DDL → 仍进补齐集合(该触发重规划)。"""
    issues = [
        "缺少 系统配置 功能对应的子任务",
        "14 张业务表缺失 SQL DDL 建表脚本",
        "未覆盖 通知公告 模块",
    ]
    got = _filter_completeness_missing(issues)
    assert len(got) == 3


def test_mixed_only_keeps_real_missing():
    """混合输入：只保留真缺功能，描述质量类放过。"""
    issues = [
        "st-15-1 描述截断缺少指引",          # 描述质量 → 放过
        "缺少 系统日志 查询功能子任务",        # 真缺功能 → 保留
        "命名建议更规范（软建议）",            # 无 missing 关键词 → 本就不算
    ]
    got = _filter_completeness_missing(issues)
    assert got == ["缺少 系统日志 查询功能子任务"]


def test_empty_and_none_safe():
    assert _filter_completeness_missing([]) == []
    assert _filter_completeness_missing(None) == []


def main():
    print("\n🧪 P6b 完整性闸门精度治本单测\n")
    tests = [
        test_description_truncation_issue_does_not_trigger_replan,
        test_real_missing_feature_still_triggers,
        test_mixed_only_keeps_real_missing,
        test_empty_and_none_safe,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
            print(f"  ✅ {t.__name__}")
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n📊 结果: {passed} 通过, {failed} 失败\n")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
