#!/usr/bin/env python3
"""R65E-T1（#82）：G1 coherence 自愈——改既有外部模块的构建清单≠新模块跨物理根。

round65e 死因（task 7297a527，FAILED@VALIDATE_PLAN，零执行）：PRD 建预警平台，plan 正确地把
alarm 业务落 ruoyi-alarm/（新模块），并按 RuoYi 单体约定把接线文件落既有模块 ruoyi-admin——
st-39-2 改 `ruoyi-admin/pom.xml`（加 ruoyi-alarm 依赖，让 app 真正包含该 feature）。但
`_resolve_module_dirs` 把 module=ruoyi-alarm 的 file_plan 里 `ruoyi-admin/pom.xml`（_EV_MANIFEST）
的 root 记成 ruoyi-admin，与业务码 root=ruoyi-alarm 凑成两根 → fp_ambiguous → G1 硬打回 →
R64-T3 熔断（同签名 2 轮）→ FAILED。（.html 模板是 _EV_AUX 已被排除，触发者只有 pom 清单。）

病根：证据分类器分不清【新建 pom 声明模块根】与【改既有模块 pom 加依赖】。区分信号 = 该目录
是否既有基线模块（project_path 下有构建清单）。改既有外部模块的构建文件是合法跨模块接线
（单体 feature 插进 app 壳的标准动作），绝不构成"新模块 ruoyi-alarm 跨到第二 build 单元"。

治本（栈中立、fail-closed）：算 M 的 fp 物理根时，排除【仅由 manifest 证据主张、且是既有基线
外部模块】的根。稳定基线判定（非 round59 new-dir 闪烁）；无 project_path 时保守不放宽（严判）；
两个都是新落点（无既有基线）仍判歧义——round62 alarm-api 双落点保护不破。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

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

_POM = """<?xml version="1.0"?>
<project><modelVersion>4.0.0</modelVersion><artifactId>ruoyi-admin</artifactId></project>
"""


def _st(sid, create_files):
    sc = FileScope(writable=[], readable=[], create_files=create_files)
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT, scope=sc)


def _plan(subtasks, modules):
    p = TaskPlan(subtasks=subtasks, parallel_groups=[[s.id for s in subtasks]])
    p.shared_contract = {"dependencies": [{"module": m} for m in modules]}
    return p


def _baseline_with_admin(tmp_path: Path) -> str:
    """造一个既有 ruoyi-admin 模块（带 pom.xml）的基线树。"""
    (tmp_path / "ruoyi-admin").mkdir()
    (tmp_path / "ruoyi-admin" / "pom.xml").write_text(_POM, encoding="utf-8")
    return str(tmp_path)


# ── ① round65e 死因本体：改既有 ruoyi-admin/pom.xml 不得使新模块 ruoyi-alarm 歧义 ──

def test_foreign_existing_module_pom_edit_does_not_make_new_module_ambiguous(tmp_path):
    base = _baseline_with_admin(tmp_path)
    plan = _plan(
        [_st("st-biz", ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"]),
         _st("st-wire", ["ruoyi-admin/pom.xml"])],  # 依赖注册进既有 app 壳
        ["ruoyi-alarm"],
    )
    fp = [
        {"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"},
        {"module": "ruoyi-alarm", "path": "ruoyi-admin/pom.xml"},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-alarm" not in ambiguous, \
        f"改既有外部模块 pom（合法接线）不得使新模块歧义（round65e 死因）: {ambiguous}"
    assert resolved.get("ruoyi-alarm") == "ruoyi-alarm", \
        f"新模块应解析到其自有源码根: {resolved}"
    r = validate_module_coherence(plan, project_path=base, file_plan=fp)
    assert r.valid, f"G1 应放行（RuoYi 单体接线合法）: {r.issues}"


# ── ② 保护：两个都是【新】落点（无既有基线）→ 仍判歧义（round62 alarm-api 保护不破）──

def test_two_new_dirs_still_ambiguous(tmp_path):
    base = str(tmp_path)  # 空基线，两个目录都不存在
    plan = _plan(
        [_st("a", ["alarm-core/src/main/java/com/x/Core.java"]),
         _st("b", ["alarm-web/src/main/java/com/x/Web.java"])],
        ["alarm-api"],
    )
    fp = [{"module": "alarm-api", "path": "alarm-core/src/main/java/com/x/Core.java"},
          {"module": "alarm-api", "path": "alarm-web/src/main/java/com/x/Web.java"}]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "alarm-api" in ambiguous, \
        f"两个新落点应保持歧义（G1 原保护不破）: resolved={resolved} amb={ambiguous}"


# ── ③ 保护：无 project_path → 无法证实既有基线 → 保守严判（fail-closed 不放宽）──

def test_no_project_path_stays_strict(tmp_path):
    plan = _plan(
        [_st("st-biz", ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"]),
         _st("st-wire", ["ruoyi-admin/pom.xml"])],
        ["ruoyi-alarm"],
    )
    fp = [
        {"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"},
        {"module": "ruoyi-alarm", "path": "ruoyi-admin/pom.xml"},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, None, fp)
    assert "ruoyi-alarm" in ambiguous, \
        f"无 project_path 无法证实既有基线 → 保守严判（不放宽 = fail-closed）: {ambiguous}"


# ── ④ 保护：改的 pom 目录【不是】既有基线模块（新建的第二模块 pom）→ 仍歧义 ──

def test_new_second_module_pom_still_ambiguous(tmp_path):
    base = str(tmp_path)  # ruoyi-extra 不存在于基线
    plan = _plan(
        [_st("st-biz", ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"]),
         _st("st-pom2", ["ruoyi-extra/pom.xml"])],  # 新建的另一个模块 pom（非既有）
        ["ruoyi-alarm"],
    )
    fp = [
        {"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"},
        {"module": "ruoyi-alarm", "path": "ruoyi-extra/pom.xml"},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-alarm" in ambiguous, \
        f"新建第二模块 pom（非既有基线）仍应歧义（不放行真跨新 build 单元）: {ambiguous}"


# ── ⑤ 复核①CONFIRMED 回归锁：纯接线模块（零代码证据）只改两个既有外部模块 pom、
#      契约标签匹配二者皆非 → 剔除会清空自有根 → 必须保持歧义硬判，绝不降级 zero-dir 软 warn ──

def test_zero_code_wiring_two_existing_poms_stays_ambiguous(tmp_path):
    # 两个既有基线模块
    for m in ("ruoyi-admin", "ruoyi-alarm-old"):
        (tmp_path / m).mkdir()
        (tmp_path / m / "pom.xml").write_text(_POM, encoding="utf-8")
    base = str(tmp_path)
    plan = _plan(
        [_st("st-w1", ["ruoyi-admin/pom.xml"]),
         _st("st-w2", ["ruoyi-alarm-old/pom.xml"])],
        ["ruoyi-alarm"],  # 契约标签匹配两个物理目录皆非，且无任何代码落点
    )
    fp = [{"module": "ruoyi-alarm", "path": "ruoyi-admin/pom.xml"},
          {"module": "ruoyi-alarm", "path": "ruoyi-alarm-old/pom.xml"}]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-alarm" in ambiguous, \
        f"纯接线无自有锚根=真违①，剔除清空自有根时必须保持歧义硬判（不降级软 warn）: " \
        f"resolved={resolved} amb={ambiguous}"
    r = validate_module_coherence(plan, project_path=base, file_plan=fp)
    assert not r.valid, "G1 应硬打回（哪个既有目录是它的家无法判）"


# ── ⑥ 猎手 B CONFIRMED 回归锁：既有基线判定必须钉扎 base_commit（git-pin），不读实时工作树 ──
#      前一轮 merge 落盘的模块（base 树里没有）绝不算"既有基线"——否则 round59 血泪跨轮版复发。

def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"]})


def _git_out(cwd, *args):
    import subprocess
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_baseline_pinned_to_base_commit_not_live_tree(tmp_path):
    # base 提交里只有 ruoyi-admin（既有基线模块）
    (tmp_path / "ruoyi-admin").mkdir()
    (tmp_path / "ruoyi-admin" / "pom.xml").write_text(_POM, encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    base_sha = _git_out(tmp_path, "rev-parse", "HEAD")
    # 模拟前一轮 merge：base 之后才落盘 ruoyi-latecomer（不在 base 树）
    (tmp_path / "ruoyi-latecomer").mkdir()
    (tmp_path / "ruoyi-latecomer" / "pom.xml").write_text(
        _POM.replace("ruoyi-admin", "ruoyi-latecomer"), encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "round1-merge")
    base = str(tmp_path)

    # A：接线进【base 树里的既有模块 ruoyi-admin】→ 放行（git-pin 认定既有）
    plan_a = _plan(
        [_st("st-biz", ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/Svc.java"]),
         _st("st-wire", ["ruoyi-admin/pom.xml"])], ["ruoyi-alarm"])
    fp_a = [{"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/Svc.java"},
            {"module": "ruoyi-alarm", "path": "ruoyi-admin/pom.xml"}]
    _, amb_a, _ = _resolve_module_dirs(plan_a, base, fp_a, base_ref=base_sha)
    assert "ruoyi-alarm" not in amb_a, f"base 树内既有模块接线应放行: {amb_a}"

    # B：接线进【base 之后才 merge 的 ruoyi-latecomer】→ 仍歧义（不读实时树，round59 闭合）
    plan_b = _plan(
        [_st("st-biz", ["ruoyi-alarm/src/main/java/com/ruoyi/alarm/Svc.java"]),
         _st("st-wire", ["ruoyi-latecomer/pom.xml"])], ["ruoyi-alarm"])
    fp_b = [{"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/Svc.java"},
            {"module": "ruoyi-alarm", "path": "ruoyi-latecomer/pom.xml"}]
    _, amb_b, _ = _resolve_module_dirs(plan_b, base, fp_b, base_ref=base_sha)
    assert "ruoyi-alarm" in amb_b, \
        f"base 后才落盘的模块绝不算既有基线（判据钉扎 base_commit 非实时树，round59 闭合）: {amb_b}"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
