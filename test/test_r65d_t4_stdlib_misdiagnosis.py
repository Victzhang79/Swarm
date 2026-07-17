"""R65D-T4：round36 自愈把 JDK 标准库类型误诊成"自造内部类型"→ 下毒指令。

round65d 毒树第一株（C 路实锤）：worker 代码缺 `import java.util.Map` → 编译报
`cannot find symbol: class Map`（无 import 证据——缺的就是 import！）→ 自愈分支③
"无 import 证据的缺失类"与 blocked 包全配对 → create_files 塞进
`.../alarm/util/Map.java` + retry_guidance 命令 worker "新建它" → worker 抗命写下
SCOPE_OBJECTION 拒工书（"该类型属 java.util，拒绝新建"）→ 拒工书当源码落盘进交付树。

治本：
① _derive_missing_type_files 对【无 import 证据】与【邻近共现】两路配对过滤
  JDK/标准库常见类型（函数已声明 JVM 专属语义，集合过滤不违栈中立铁律）；
  显式 import 证据（`import com.x.util.Map` 指向 blocked 包）仍放行——证据赢过名单。
② 误诊改道：全部缺失类都是标准库类型时，自愈不再无从下手连坐放弃，而是注入
  "缺 import 语句"指导重派（治得了的病绝不放弃；绝不再下"新建同名类型"毒指令）。
"""
import asyncio
from unittest.mock import patch

from swarm.brain.nodes import handle_failure
from swarm.brain.nodes.failure import _derive_missing_type_files
from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput

_JF = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"

_BO_MISSING_IMPORT = (
    "[ERROR] cannot find symbol\n"
    "  symbol:   class Map\n"
    "  location: class com.ruoyi.alarm.service.AlarmService\n"
    "[ERROR] cannot find symbol\n"
    "  symbol:   class HashMap\n"
    "  location: var registry\n"
)

_BO_REAL_CUSTOM = (
    "[ERROR] cannot find symbol\n"
    "  symbol:   class TwoFactorSetupVO\n"
    "  location: var user\n"
)


def test_stdlib_type_without_import_evidence_not_created():
    """★毒株本体★：Map/HashMap 无 import 证据=缺的就是 import，绝不推导新建文件。"""
    files = _derive_missing_type_files(
        [_JF], ["com.ruoyi.alarm.util"], _BO_MISSING_IMPORT)
    assert not any("Map" in f for f in files), \
        f"JDK 类型绝不可被诊断成待建内部类型（round65d Map.java 拒工书死型）: {files}"


def test_explicit_import_evidence_overrides_stdlib_name():
    """证据赢过名单：`import <blocked>.Map` 明示项目内自定义 Map → 照常推导新建。"""
    bo = ("import com.ruoyi.alarm.util.Map;\n"
          "[ERROR] cannot find symbol\n  symbol:   class Map\n"
          "  location: class AlarmService\n")
    files = _derive_missing_type_files([_JF], ["com.ruoyi.alarm.util"], bo)
    assert any(f.endswith("com/ruoyi/alarm/util/Map.java") for f in files), \
        f"显式 import 证据指向 blocked 包=真自定义类型，不受名单误伤: {files}"


def test_real_custom_type_still_derived():
    """对照面：真自造类型（TwoFactorSetupVO 非标准库）照旧推导（round36 语义不回归）。"""
    files = _derive_missing_type_files(
        [_JF], ["com.ruoyi.alarm.vo"], _BO_REAL_CUSTOM)
    assert any(f.endswith("TwoFactorSetupVO.java") for f in files), files


def test_cross_package_coincident_name_still_shadowed():
    """★复核 HIGH 锁（带复现）★：dto 包一条断裂 import（package-does-not-exist 回显
    `import com.x.dto.Date;`）绝不全局解除 Date 在 util 包上的遮蔽——证据按
    【类名×包】配对，util 包缺 import 的 Date 绝不被下新建指令。"""
    bo = (
        "src/main/java/com/ruoyi/alarm/dto/Filter.java:4: error: "
        "package com.ruoyi.alarm.dto does not exist\n"
        "import com.ruoyi.alarm.dto.Date;\n                          ^\n"
        "src/main/java/com/ruoyi/alarm/util/RegistryHelper.java:12: error: "
        "cannot find symbol\n"
        "  symbol:   class Date\n"
        "  location: class com.ruoyi.alarm.util.RegistryHelper\n"
    )
    files = _derive_missing_type_files(
        [_JF], ["com.ruoyi.alarm.dto", "com.ruoyi.alarm.util"], bo)
    assert not any(f.endswith("util/Date.java") for f in files), \
        f"跨包巧合同名绝不解除遮蔽（Map.java 死型经双 blocked 包复活的路径）: {files}"
    assert any(f.endswith("dto/Date.java") for f in files), \
        f"显式 import 证据指向 dto=dto 的 Date 是真自定义类型，照常推导: {files}"


def test_diversion_prunes_stale_poison_create_files():
    """★猎手 HIGH 锁★：改道时清除旧轮误诊塞进 create_files 的 JDK 同名毒文件声明
    （checkpoint 跨部署恢复面）——绝不让 worker 同时收到'补 import'与'可新建
    Map.java'两道矛盾指令。"""
    st12 = SubTask(id="st-12", description="d",
                   scope=FileScope(
                       writable=[_JF],
                       create_files=[
                           "ruoyi-alarm/src/main/java/com/ruoyi/alarm/util/Map.java",
                           "ruoyi-alarm/src/main/java/com/ruoyi/alarm/vo/RealVO.java",
                       ]))
    plan = TaskPlan(subtasks=[st12])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {
            "st-12": _wo_blocked("st-12", ["com.ruoyi.alarm.util"],
                                 _BO_MISSING_IMPORT)},
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable",
               return_value=True):
        r = asyncio.run(handle_failure(state))
    assert "st-12" in (r.get("dispatch_remaining") or []), r
    cf = st12.scope.create_files or []
    assert not any(f.endswith("/Map.java") for f in cf), \
        f"残留毒 create_files 必须清除: {cf}"
    assert any(f.endswith("/RealVO.java") for f in cf), \
        f"非毒声明绝不误删: {cf}"


def _wo_blocked(sid, pkgs, bo):
    return WorkerOutput(
        subtask_id=sid, diff="", summary="", l1_passed=False,
        l1_details={"pipeline_blocked": "internal_pkg_not_built",
                    "blocked_on_packages": pkgs, "not_run_kind": "blocked",
                    "failure_class": "transient", "build_output": bo},
        confidence="low")


def test_stdlib_only_miss_redirects_to_import_guidance():
    """★改道面★：缺失类全是标准库类型 → 不连坐放弃、不下新建指令，注入
    「补 import」指导重派（治得了的病绝不放弃）。"""
    st12 = SubTask(id="st-12", description="d",
                   scope=FileScope(writable=[_JF]))
    plan = TaskPlan(subtasks=[st12])
    state = {
        "failed_subtask_ids": ["st-12"],
        "subtask_results": {
            "st-12": _wo_blocked("st-12", ["com.ruoyi.alarm.util"],
                                 _BO_MISSING_IMPORT)},
        "dispatch_remaining": [],
        "plan": plan,
    }
    with patch("swarm.brain.nodes.failure._blocked_pkg_unrecoverable",
               return_value=True):
        r = asyncio.run(handle_failure(state))
    assert "st-12" not in set(r.get("abandoned_subtask_ids") or []), \
        f"缺 import 是治得了的病，绝不连坐放弃: {r.get('abandoned_subtask_ids')}"
    assert "st-12" in (r.get("dispatch_remaining") or []), r
    assert "import" in (st12.retry_guidance or ""), \
        f"必须注入补 import 指导: {st12.retry_guidance!r}"
    assert "新建" not in (st12.retry_guidance or "") \
        or "绝不" in (st12.retry_guidance or ""), \
        "绝不再下'新建同名类型'毒指令"
    assert not any("Map" in f for f in (st12.scope.create_files or [])), \
        f"create_files 绝不被塞进 JDK 同名文件: {st12.scope.create_files}"
