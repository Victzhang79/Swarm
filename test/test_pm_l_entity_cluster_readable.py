"""P-L1~3（27 号文）：实体聚簇 readable/stem 表扩栈。

两臂：
① enrich_java_package_readable → enrich_package_dir_readable：只认 .java → 认
   STACK_SPEC 全部源码扩展名（Go 同目录=同 package 最可惜；Python 同目录同命名空间）。
   消费契约保持【同扩展名】保守匹配（.kt 目标不拉 .java 兄弟）。
② _entity_stem 扩展名白名单（java|xml|sql|vue|js|ts|go|py）→ 通用末段剥离：
   .tsx/.kt/.rs/.php 不再让整个实体聚簇失效。
"""
from __future__ import annotations

import os

from swarm.brain.contract_utils import enrich_package_dir_readable
from swarm.brain.planning_nodes import _entity_stem
from swarm.stacks import STACK_SPEC
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan


def _mk(root, rel, content="x"):
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def _plan_with(create):
    st = SubTask(id="st-1", description="t", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(create_files=list(create), writable=[], readable=[]),
                 acceptance_criteria=[])
    plan = TaskPlan(subtasks=[st], parallel_groups=[["st-1"]])
    plan.shared_contract = {}
    return plan


# ── ① 同目录兄弟源码 readable 扩栈 ──────────────────────────────

def test_go_siblings_enriched(tmp_path):
    """★主治锁（Go 尤其可惜）★：写目标 main.go 的同目录兄弟 .go 全部入 readable。"""
    _mk(tmp_path, "cmd/server/main.go")
    _mk(tmp_path, "cmd/server/handler.go")
    _mk(tmp_path, "cmd/server/config.go")
    plan = _plan_with(["cmd/server/main.go"])
    assert enrich_package_dir_readable(plan, str(tmp_path)) is True
    readable = plan.subtasks[0].scope.readable
    assert "cmd/server/handler.go" in readable
    assert "cmd/server/config.go" in readable


def test_python_siblings_enriched(tmp_path):
    _mk(tmp_path, "report/services/user_service.py")
    _mk(tmp_path, "report/services/report_query.py")
    plan = _plan_with(["report/services/user_service.py"])
    assert enrich_package_dir_readable(plan, str(tmp_path)) is True
    assert "report/services/report_query.py" in plan.subtasks[0].scope.readable


def test_java_behavior_byte_identical(tmp_path):
    """★回归锁★：Java 臂与原实现逐字节一致（同包 .java 兄弟入 readable）。"""
    _mk(tmp_path, "ruoyi/src/main/java/com/x/StringUtils.java")
    _mk(tmp_path, "ruoyi/src/main/java/com/x/Constants.java")
    _mk(tmp_path, "ruoyi/src/main/java/com/x/StrFormatter.java")
    plan = _plan_with(["ruoyi/src/main/java/com/x/StringUtils.java"])
    assert enrich_package_dir_readable(plan, str(tmp_path)) is True
    readable = plan.subtasks[0].scope.readable
    assert set(readable) == {"ruoyi/src/main/java/com/x/Constants.java",
                             "ruoyi/src/main/java/com/x/StrFormatter.java"}


def test_cross_ext_not_pulled(tmp_path):
    """消费契约锁：同目录【异扩展名】不拉（.ts 目标不拉 .tsx/.java 兄弟——保守同构，
    跨扩展名是二期精确 import 图的事）。"""
    _mk(tmp_path, "web/src/App.ts")
    _mk(tmp_path, "web/src/Button.tsx")
    _mk(tmp_path, "web/src/Note.java")
    plan = _plan_with(["web/src/App.ts"])
    assert enrich_package_dir_readable(plan, str(tmp_path)) is False
    assert plan.subtasks[0].scope.readable == []


def test_non_source_target_noop(tmp_path):
    """非源码写目标（资源/清单/文档）不触发拉取——扩展名集派生自 STACK_SPEC，
    .xml/.sql/.md 不在 source_exts 里。"""
    _mk(tmp_path, "mod/src/main/resources/mapper/UserMapper.xml")
    _mk(tmp_path, "mod/src/main/resources/mapper/OrderMapper.xml")
    plan = _plan_with(["mod/src/main/resources/mapper/UserMapper.xml"])
    assert enrich_package_dir_readable(plan, str(tmp_path)) is False


def test_sibling_pull_capped_with_warning(tmp_path, caplog):
    """★hunter F1 锁★：单目录 60 个 .go 兄弟 → 截断到上限 50（字典序最小者）+
    WARNING 机读可辨——Go 大 package 不再把 worker 上下文撑爆。"""
    _mk(tmp_path, "pkg/main.go")
    for i in range(60):
        _mk(tmp_path, f"pkg/f{i:03d}.go")
    plan = _plan_with(["pkg/main.go"])
    import logging
    with caplog.at_level(logging.WARNING):
        assert enrich_package_dir_readable(plan, str(tmp_path)) is True
    readable = plan.subtasks[0].scope.readable
    assert len(readable) == 50
    assert readable == sorted(readable), "截断后按字典序（确定性）"
    assert "pkg/f000.go" in readable and "pkg/f049.go" in readable
    assert "pkg/f050.go" not in readable
    assert any("超限截断" in r.message and "60" in r.message
               for r in caplog.records), "截断必须 WARNING 机读可辨，绝不静默丢"


def test_src_exts_derived_from_stack_spec():
    """单一事实源锁：新栈加表行即自动生效（行为级探针：每个栈的首个源码扩展名
    都真造目录场景验证，非 getsource 断实现）。"""
    import tempfile
    for spec in STACK_SPEC.values():
        ext = spec.source_exts[0]
        with tempfile.TemporaryDirectory() as td:
            _mk(td, f"pkg/a{ext}")
            _mk(td, f"pkg/b{ext}")
            plan = _plan_with([f"pkg/a{ext}"])
            assert enrich_package_dir_readable(plan, td) is True, \
                f"栈 {spec.key} 的源码扩展名 {ext} 未触发同目录拉取"


# ── ② _entity_stem 扩展名通用剥离 ──────────────────────────────

def test_entity_stem_new_stack_extensions():
    """★主治锁★：治前白名单外的扩展名也能剥掉 → 实体词干聚簇不再按扩展名碎裂。"""
    assert _entity_stem("web/src/UserCard.tsx") == "UserCard"
    assert _entity_stem("mod/src/main/kotlin/AlarmService.kt") == "Alarm"
    assert _entity_stem("src/user_service.rs") == "user_service"
    assert _entity_stem("app/Http/UserController.php") == "User"


def test_entity_stem_legacy_byte_identical():
    """★回归锁★：白名单内扩展名行为逐字节不变（含 I 前缀与层后缀剥离）。"""
    assert _entity_stem("a/b/AlarmApp.java") == "AlarmApp"
    assert _entity_stem("a/b/AlarmAppMapper.xml") == "AlarmApp"
    assert _entity_stem("a/b/IAlarmAppService.java") == "AlarmApp"
    assert _entity_stem("a/b/AlarmAppController.java") == "AlarmApp"
    assert _entity_stem("a/b/alarm_app.py") == "alarm_app"
    assert _entity_stem("a/b/alarm-app.vue") == "alarm-app"


def test_entity_stem_dotfile_and_noext_preserved():
    """边界：点开头/无点文件名原样保留（不剥成空串）。"""
    assert _entity_stem("a/.gitkeep") == ".gitkeep"
    assert _entity_stem("a/Makefile") == "Makefile"
