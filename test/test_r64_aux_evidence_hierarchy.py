#!/usr/bin/env python3
"""R64：模块物理根的★证据强度分层★——辅助文件绝不定义/扩张模块物理根。

round64 死因（task f1e0f7b5，FAILED@PLAN，G1 三验三拒）：PRD 要求 DDL，RuoYi 惯例 sql 落
仓库顶层 sql/（合法棕地布局），GLM 三轮 plan 都把 sql/*.sql 的 file_plan.module 标成对应
功能模块（语义正确）。但 `_resolve_module_dirs` 的 fp 多根判定对无源码布局段的文件回退
【顶层目录】当物理根（Task#9 silent-hunter #1 为 flat 项目加的兜底）→ ruoyi-admin 的
roots={'ruoyi-admin','sql'} → 违① 硬打回；issues 反馈（"归到同一模块目录"）对该布局
结构性不可满足 → LLM 重试永不收敛 → FAILED。两条治本互拆（round60 同型）。

治本＝证据强度分层（栈中立、disk-independent、确定性）：
  - 强证据＝路径含标准源码布局段（_SRC_LAYOUT_SEGMENTS，Maven/Gradle/Cargo/Go/Node 通用）
    的文件——只有它们定义模块物理根。
  - 弱证据＝无任何布局信号的文件（sql/docs/scripts/构建清单）——有强证据时不参与定根与
    歧义判定（它们是逻辑归属模块的辅助交付物，不参与构建 reactor，不需要 pom）。
  - flat/纯脚本项目（全体皆弱）→ 保持原判（silent-hunter #1 兜底不放水）。
  - 两条证据通道（file_plan 权威 + 子任务 scope 名字匹配）同一规则，不留同族暗门。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.brain.contract_utils import _resolve_module_dirs  # noqa: E402
from swarm.brain.plan_validator import validate_module_coherence  # noqa: E402
from swarm.types import (  # noqa: E402
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskPlan,
)


def _st(sid, create_files):
    sc = FileScope(writable=[], readable=[], create_files=create_files)
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT, scope=sc)


def _plan(subtasks, modules):
    p = TaskPlan(subtasks=subtasks, parallel_groups=[[s.id for s in subtasks]])
    p.shared_contract = {"dependencies": [{"module": m} for m in modules]}
    return p


# ── ① file_plan 通道：round64 病根本体 ──

def test_fp_toplevel_sql_does_not_split_module():
    """★round64 本体★ 模块有源码强证据 + 顶层 sql 弱证据 → 绝不判多物理根。"""
    plan = _plan([_st("a", ["ruoyi-admin/src/main/java/com/ruoyi/web/AlarmController.java"])],
                 ["ruoyi-admin"])
    fp = [{"module": "ruoyi-admin", "path": "ruoyi-admin/src/main/java/com/ruoyi/web/AlarmController.java"},
          {"module": "ruoyi-admin", "path": "sql/ry_alarm_20260716.sql"}]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, None, fp)
    assert "ruoyi-admin" not in ambiguous, \
        f"顶层辅助 sql 不得扩张模块物理根（round64 死因）: {ambiguous}"
    assert resolved.get("ruoyi-admin") == "ruoyi-admin", \
        f"强证据应把模块根解析到源码根: {resolved}"
    r = validate_module_coherence(plan, file_plan=fp)
    assert r.valid, r.issues


def test_fp_multiple_aux_dirs_still_no_split():
    """多个顶层辅助目录（sql + docs + scripts）也不构成歧义——弱证据整类退位。"""
    plan = _plan([_st("a", ["ruoyi-alarm/alarm-core/src/main/java/Core.java"])],
                 ["alarm-core"])
    fp = [{"module": "alarm-core", "path": "ruoyi-alarm/alarm-core/src/main/java/Core.java"},
          {"module": "alarm-core", "path": "sql/alarm_ddl.sql"},
          {"module": "alarm-core", "path": "docs/alarm-design.md"},
          {"module": "alarm-core", "path": "scripts/init_alarm.sh"}]
    resolved, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "alarm-core" not in ambiguous
    assert resolved.get("alarm-core") == "ruoyi-alarm/alarm-core"


def test_fp_dual_code_roots_still_ambiguous():
    """真·双源码根（违① 本体）照打回——弱证据退位绝不放走真病。"""
    plan = _plan([_st("a", ["alarm-api/src/main/java/A.java"])], ["alarm-api"])
    fp = [{"module": "alarm-api", "path": "alarm-api/src/main/java/A.java"},
          {"module": "alarm-api", "path": "ruoyi-alarm/alarm-api/src/main/java/B.java"},
          {"module": "alarm-api", "path": "sql/x.sql"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "alarm-api" in ambiguous
    assert set(ambiguous["alarm-api"]) == {"alarm-api", "ruoyi-alarm/alarm-api"}, \
        f"歧义清单只列源码根，辅助目录不掺入: {ambiguous}"


def test_fp_flat_project_multi_dir_still_caught():
    """flat/纯脚本项目（全体皆弱）→ 多顶层目录仍判歧义（silent-hunter #1 兜底不放水）。"""
    plan = TaskPlan(subtasks=[_st("a", ["svc-a/app.py"])], parallel_groups=[["a"]])
    plan.shared_contract = {"dependencies": [{"module": "svc-a"}]}
    fp = [{"module": "svc-a", "path": "svc-a/app.py"},
          {"module": "svc-a", "path": "svc-a-legacy/deploy.py"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "svc-a" in ambiguous, "flat 项目的多根歧义绝不因本治本放水"


def test_fp_aux_only_module_keeps_old_behavior():
    """纯辅助文件模块（如 db-scripts 只含 sql）→ 无强证据，回退顶层目录，行为不变。"""
    plan = TaskPlan(subtasks=[_st("a", ["sql/a.sql"])], parallel_groups=[["a"]])
    fp = [{"module": "db-scripts", "path": "sql/a.sql"},
          {"module": "db-scripts", "path": "sql/b.sql"}]
    resolved, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert resolved.get("db-scripts") == "sql"
    assert "db-scripts" not in ambiguous


def test_fp_root_src_plus_sql_single_module_passes():
    """根级 src 布局（单模块仓库）+ 顶层 sql → 同族病：不得判歧义（模块根=仓库根，无从分裂）。"""
    plan = TaskPlan(subtasks=[_st("a", ["src/main/java/X.java"])], parallel_groups=[["a"]])
    fp = [{"module": "app", "path": "src/main/java/X.java"},
          {"module": "app", "path": "sql/init.sql"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "app" not in ambiguous, f"根级源码 + 顶层辅助文件是合法单模块布局: {ambiguous}"
    r = validate_module_coherence(plan, file_plan=fp)
    assert r.valid, r.issues


def test_fp_stack_neutral_node_docs():
    """栈中立锁：Node 布局（web/src/…）+ 顶层 docs → 同规则生效，不特判任何栈/目录名。"""
    plan = TaskPlan(subtasks=[_st("a", ["web/src/components/Alarm.vue"])],
                    parallel_groups=[["a"]])
    fp = [{"module": "web", "path": "web/src/components/Alarm.vue"},
          {"module": "web", "path": "docs/frontend.md"}]
    resolved, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "web" not in ambiguous
    assert resolved.get("web") == "web"


# ── ①b 对抗双复核整改锁（CR-H1 / 猎手 F1/F2/F3/F5） ──

def test_fp_rootlevel_plus_subdir_dual_strong_still_ambiguous():
    """★复核 CR-H1 锁★ 根级布局根（src/ 直居仓库根，记 "."）+ 子目录根 = 真双根违①，
    绝不因根级根无目录名而静默消失（否则 G1 从硬拦降级成 zero-dir warn）。"""
    plan = TaskPlan(subtasks=[_st("a", ["src/main/java/com/x/RootFile.java"])],
                    parallel_groups=[["a"]])
    fp = [{"module": "app", "path": "src/main/java/com/x/RootFile.java"},
          {"module": "app", "path": "subdir/src/main/java/com/x/OtherFile.java"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert ambiguous.get("app") == [".", "subdir"], \
        f"根级强根被静默丢弃=真双根违①漏判: {ambiguous}"


def test_fp_flat_code_plus_layout_code_still_ambiguous():
    """★猎手 F1 锁★ flat 布局真源码（web/App.js，无布局段）+ src 布局源码（libs/web/src/…）
    = 混合布局真双根，flat 源码绝不因"无布局段"被当辅助物退位。"""
    plan = TaskPlan(subtasks=[_st("a", ["web/App.js"])], parallel_groups=[["a"]])
    fp = [{"module": "web", "path": "web/App.js"},
          {"module": "web", "path": "libs/web/src/index.js"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert ambiguous.get("web") == ["libs/web", "web"], \
        f"flat 真源码被误当辅助物退位=真双根静默放行: {ambiguous}"


def test_fp_aux_ext_beats_layout_segment_collision():
    """★猎手 F2 锁★ 'sql/main/x.sql' 的 'main' 撞布局段词表——辅助扩展名判定必须先于
    布局段判定，否则 round64 死法换个目录名原样复活。"""
    plan = _plan([_st("a", ["ruoyi-admin/src/main/java/X.java"])], ["ruoyi-admin"])
    fp = [{"module": "ruoyi-admin", "path": "ruoyi-admin/src/main/java/X.java"},
          {"module": "ruoyi-admin", "path": "sql/main/ry_alarm.sql"},
          {"module": "ruoyi-admin", "path": "sql/test/fixture.sql"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "ruoyi-admin" not in ambiguous, \
        f"辅助扩展名被布局段词表升格=round64 复活: {ambiguous}"


def test_fp_manifest_in_wrong_dir_still_ambiguous():
    """★猎手 F3 锁★ 清单=「声明的构建根」：清单声明在错误目录（与源码根不一致）必须
    照判违①——清单证据退位会让 LLM 坐标幻觉类病静默穿闸。"""
    plan = TaskPlan(subtasks=[_st("a", ["alarm-core/src/main/java/Core.java"])],
                    parallel_groups=[["a"]])
    fp = [{"module": "alarm-core", "path": "alarm-core/src/main/java/Core.java"},
          {"module": "alarm-core", "path": "wrong-dir/alarm-core/pom.xml"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert ambiguous.get("alarm-core") == ["alarm-core", "wrong-dir/alarm-core"], \
        f"清单声明错目录未被检出: {ambiguous}"


def test_fp_manifest_in_module_dir_no_false_positive():
    """清单在模块自己目录（嵌套模块自领 pom，R58-3 常态）→ 清单根=源码根，不误判。"""
    plan = TaskPlan(subtasks=[_st("a", ["ruoyi-alarm/alarm-core/src/main/java/C.java"])],
                    parallel_groups=[["a"]])
    fp = [{"module": "alarm-core", "path": "ruoyi-alarm/alarm-core/src/main/java/C.java"},
          {"module": "alarm-core", "path": "ruoyi-alarm/alarm-core/pom.xml"}]
    _, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert "alarm-core" not in ambiguous, f"嵌套模块自领 pom 被误判: {ambiguous}"


def test_fp_manifest_only_module_prefix_preserved():
    """清单-only 模块（聚合父只带自己的 pom）→ prefix 解析保持旧行为（fp 权威可解析）。"""
    plan = TaskPlan(subtasks=[_st("a", ["ruoyi-alarm/pom.xml"])], parallel_groups=[["a"]])
    fp = [{"module": "ruoyi-alarm", "path": "ruoyi-alarm/pom.xml"}]
    resolved, ambiguous, _ = _resolve_module_dirs(plan, None, fp)
    assert resolved.get("ruoyi-alarm") == "ruoyi-alarm"
    assert "ruoyi-alarm" not in ambiguous


def test_evidence_demotion_is_observable(caplog):
    """★猎手 F5 锁★ 辅助证据退位必须留痕（fail-open 可观测铁律）——否则"闸正确解析"与
    "闸静默扔了一桶矛盾证据"日志上不可分。"""
    import logging

    plan = _plan([_st("a", ["ruoyi-admin/src/main/java/X.java"])], ["ruoyi-admin"])
    fp = [{"module": "ruoyi-admin", "path": "ruoyi-admin/src/main/java/X.java"},
          {"module": "ruoyi-admin", "path": "sql/ry_alarm.sql"}]
    with caplog.at_level(logging.INFO, logger="swarm.brain.contract_utils"):
        _resolve_module_dirs(plan, None, fp)
    assert any("R64-EVIDENCE" in r.message and "ruoyi-admin" in r.message
               for r in caplog.records), "辅助证据退位无留痕"


# ── ② scope 名字匹配通道：同族暗门一并封死 ──

def test_scope_channel_aux_path_does_not_split_module():
    """scope 通道同规则：sql/<模块名>/… 这类弱证据不得给名字匹配通道造第二物理根。"""
    plan = _plan(
        [_st("a", ["ruoyi-admin/src/main/java/X.java"]),
         _st("b", ["sql/ruoyi-admin/init.sql"])],
        ["ruoyi-admin"])
    resolved, ambiguous, _ = _resolve_module_dirs(plan, None, None)
    assert "ruoyi-admin" not in ambiguous, \
        f"名字匹配通道的弱证据暗门未封死（同族病）: {ambiguous}"
    assert resolved.get("ruoyi-admin") == "ruoyi-admin"


def test_scope_channel_flat_name_match_still_works():
    """flat 项目 scope 名字匹配（全弱证据）仍解析——治本不砍 flat 项目的唯一证据来源。"""
    plan = _plan([_st("a", ["svc-a/app.py"])], ["svc-a"])
    resolved, _, _ = _resolve_module_dirs(plan, None, None)
    assert resolved.get("svc-a") == "svc-a"


# ── ③ 真 round64 fixture 回归（RED→GREEN 的实锤面） ──

def test_g1_accepts_round64_cassette():
    """回归：真 round64 plan（cassette f1e0f7b5）治后必过 G1——它唯一的病是 sql 弱证据分根。"""
    cf = Path(__file__).resolve().parents[1] / "cassettes" / \
        "f1e0f7b5-3be8-438e-8c07-fef2dc5588a6.json"
    if not cf.exists():
        pytest.skip("cassette 不在本机")
    c = json.loads(cf.read_text())
    plan = TaskPlan.model_validate(c["plan"])
    fp = c.get("file_plan") or []
    # 前提自证：这个 plan 确实带顶层 sql 弱证据（防 fixture 漂移后测试空转假绿）
    assert any(str(it.get("path", "")).startswith("sql/") for it in fp), \
        "fixture 前提漂移：file_plan 里已无顶层 sql 文件"
    r = validate_module_coherence(plan, file_plan=fp)
    # R67-T7a 说明：该 cassette 的 st-1 desc 里真实存在"禁 lombok vs 模板含 lombok"的
    # 考卷矛盾文本（live 管线中 R65E10-T2 剪除+考卷同源重生成会在 VALIDATE 前消解；静态
    # 夹具绕过了该步）——③d 防御纵深对此打回是正确行为，非 #40 回归。本测试只守 #40
    # 语义（G1 物理根误杀），故剔除 ③d 账目后断言。
    _non_exam = [i for i in r.issues if "考卷自相矛盾" not in i]
    assert not _non_exam, f"round64 死因未治愈: {_non_exam}"
