"""R65E7（round65e7 task 044f2caa 实锤）：file_plan 需求覆盖上游根治闸。

死因（terminal FAILED@PLAN，code+cassette+log 三坐实）：tech_design 从【PRD 原文】产 file_plan，
requirement_items（99 条）另路抽取，二者间【无覆盖交叉核验】。req-4067d7fb(Google 2FA)/
req-734ed6f4(SHA512)/代码生成器等在 183 file_plan 里【0 文件】→ 无子任务能覆盖 → planner 只能谎报
baseline_covered → T1 正确连拦 4 pass → 恢复环无法 materialize（P1 build_coverage_matrix 把 T1-拒的
baseline 仍当已覆盖=盲区，全量重拆丢覆盖回归）→ 3 retry 耗尽 → CONFIRM fail-fast → FAILED@PLAN。

治本 L2（上游根治，证据 token 与 T1 同源，栈无关）：EXTRACT+TECH_DESIGN 后确定性核验——
build_planned_vocab(file_plan)（路径 stem+CamelCase 缩略+module+responsibility），对每条 requirement：
- 零 token（纯中文无 ASCII 判别词）→ 豁免（round37 过严教训）；
- token 命中 planned_vocab → 已排文件，放行；
- 未排文件但命中 baseline_vocab → 合法存量，放行（不逼为存量能力排文件）；
- 未排文件【且】非存量 → unplanned → 定向反馈设计 LLM 补排文件。
fail-open：planned_vocab 或 baseline_vocab 任一空 → 不判（缺数据绝不臆造工作，同 T1 纪律）。
"""
from __future__ import annotations

from swarm.brain.baseline_candidates import (
    build_planned_vocab,
    requirements_missing_from_plan,
)

REQ_2FA = "req-4067d7fb"
REQ_SHA = "req-734ed6f4"
REQ_ALARM = "req-a1a1a1a1"
REQ_EXCEL = "req-e2e2e2e2"
REQ_NOTICE = "req-9a624b2b"


def _items():
    return [
        {"id": REQ_2FA, "text": "支持Google 2FA双因素认证，包含绑定/解绑/验证。", "kind": "functional"},
        {"id": REQ_SHA, "text": "密码使用SHA512加密存储。", "kind": "functional"},
        {"id": REQ_ALARM, "text": "预警任务AlarmTask的CRUD与调度。", "kind": "functional"},
        {"id": REQ_EXCEL, "text": "用户管理支持Excel导入导出。", "kind": "functional"},
        {"id": REQ_NOTICE, "text": "通知公告支持发布、撤回、已读。", "kind": "functional"},
    ]


def _file_plan():
    # 真实形态：只排了 alarm 相关文件——2FA/SHA512 全无落点（复现 round65e7 的 0 文件）
    return [
        {"path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/AlarmTask.java",
         "module": "ruoyi-alarm", "responsibility": "预警任务实体"},
        {"path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmTaskService.java",
         "module": "ruoyi-alarm", "responsibility": "预警任务调度与CRUD服务"},
    ]


def _baseline_vocab():
    # 基线（RuoYi 存量）有 Excel 工具，但【无】2FA/SHA512/AlarmTask
    from swarm.brain.baseline_candidates import build_baseline_vocab
    return build_baseline_vocab(
        [{"file_path": "ruoyi-common/utils/poi/ExcelUtil.java", "module_name": "ruoyi-common"}],
        [{"file_path": "ruoyi-common/utils/poi/ExcelUtil.java",
          "symbol_name": "importExcel", "class_name": "ExcelUtil"}])


# ── build_planned_vocab ──
def test_planned_vocab_contains_stems_module_responsibility():
    v = build_planned_vocab(_file_plan()).lower()
    assert "alarmtask" in v                    # 路径 stem
    assert "ruoyi-alarm" in v                   # module
    assert "预警任务" in v or "crud" in v        # responsibility 文本
    assert "twofactor" not in v and "2fa" not in v and "sha512" not in v


def test_planned_vocab_empty_on_empty_plan():
    assert build_planned_vocab([]) == ""
    assert build_planned_vocab(None) == ""


# ── requirements_missing_from_plan（核心） ──
def test_2fa_unplanned_flagged():
    """★RED 核★ 2FA 在 file_plan 0 文件、baseline 也无 → unplanned，逼上游补排文件。"""
    missing = requirements_missing_from_plan(
        _items(), build_planned_vocab(_file_plan()), _baseline_vocab())
    assert REQ_2FA in missing, f"2FA 无文件无存量应判 unplanned；实得 {missing}"
    assert REQ_SHA in missing, f"SHA512 无文件无存量应判 unplanned；实得 {missing}"


def test_planned_req_not_flagged():
    """AlarmTask 已排文件 → 不判 unplanned（不误报已排需求）。"""
    missing = requirements_missing_from_plan(
        _items(), build_planned_vocab(_file_plan()), _baseline_vocab())
    assert REQ_ALARM not in missing, f"已排文件的 alarm 不该判 unplanned；实得 {missing}"


def test_baseline_satisfied_req_not_flagged():
    """Excel 无 file_plan 落点但基线 ExcelUtil 存量 → 不判 unplanned（不为存量能力逼排文件）。"""
    missing = requirements_missing_from_plan(
        _items(), build_planned_vocab(_file_plan()), _baseline_vocab())
    assert REQ_EXCEL not in missing, f"存量满足的 excel 不该判 unplanned；实得 {missing}"


def test_pure_chinese_req_exempt():
    """通知公告纯中文无 ASCII 判别 token → 豁免（round37 过严教训，绝不误报）。"""
    missing = requirements_missing_from_plan(
        _items(), build_planned_vocab(_file_plan()), _baseline_vocab())
    assert REQ_NOTICE not in missing, f"纯中文需求应豁免；实得 {missing}"


def test_2fa_planned_via_responsibility_not_flagged():
    """★不回归锁★ 若 file_plan 真为 2FA 排了文件（responsibility 含 2FA）→ 不再判 unplanned。"""
    fp = _file_plan() + [{
        "path": "ruoyi-admin/src/main/java/com/ruoyi/web/controller/TwoFactorController.java",
        "module": "ruoyi-admin", "responsibility": "Google 2FA 双因素认证：绑定/解绑/验证"}]
    missing = requirements_missing_from_plan(_items(), build_planned_vocab(fp), _baseline_vocab())
    assert REQ_2FA not in missing, f"已为 2FA 排文件后不该判 unplanned；实得 {missing}"


# ── fail-open（缺数据绝不臆造工作，与 T1 同纪律） ──
def test_empty_planned_vocab_fail_open():
    """file_plan 空/vocab 空 → 不判（fail-open，不因缺 file_plan 逼全量补排）。"""
    assert requirements_missing_from_plan(_items(), "", _baseline_vocab()) == []
    assert requirements_missing_from_plan(_items(), None, _baseline_vocab()) == []


def test_empty_baseline_vocab_fail_open():
    """baseline_vocab 空（KB 不可达）→ 不判（无法区分新特性 vs 存量，保守豁免，绝不过度逼排文件）。"""
    assert requirements_missing_from_plan(_items(), build_planned_vocab(_file_plan()), "") == []
    assert requirements_missing_from_plan(_items(), build_planned_vocab(_file_plan()), None) == []


def test_no_requirement_items_returns_empty():
    assert requirements_missing_from_plan([], build_planned_vocab(_file_plan()), _baseline_vocab()) == []
    assert requirements_missing_from_plan(None, build_planned_vocab(_file_plan()), _baseline_vocab()) == []
