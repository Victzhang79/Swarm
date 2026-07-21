#!/usr/bin/env python3
"""R65E14-T1（#40）：G1 coherence 自愈——MODIFY 既有外部模块的【既有源码文件】≠新模块跨物理根。

round65e14 死因（task 7d5016e0，FAILED@VALIDATE_PLAN，零执行）：PRD 建预警平台（含 AppKey/2FA
认证），plan 正确地把 admin 业务落 ruoyi-admin/，并按 RuoYi 单体 Shiro 约定把认证接线写进既有
框架文件——MODIFY `ruoyi-framework/src/main/java/com/ruoyi/framework/config/ShiroConfig.java`
（往 Shiro 过滤器链注册新认证 filter，单体架构接新认证方式的必经 fan-in 点）。但
`_resolve_module_dirs` 把 module=ruoyi-admin 的 file_plan 里 ShiroConfig.java（_EV_STRONG）的
root 记成 ruoyi-framework，与业务码 root=ruoyi-admin 凑成两根 → fp_ambiguous → G1 硬打回 →
R64-T3 熔断（同签名 2 轮）→ FAILED。

病根：R65E-T1（#82）只豁免了【manifest】证据（改既有外部模块 pom=合法接线），源码级证据
一律保留为根——但"MODIFY 一个基线已存在的外部源码文件"与"新建源码进外部模块"是两回事：
前者文件的家早已确定（属外部模块），本模块只是去改它（注册/接线），不产生"本模块的家在哪"
的歧义；后者才是真跨模块 smell（新文件该归谁无法判）。区分信号 = 该证据文件是否存在于
【任务钉扎 base 树】（_exists_in_repo，git-pin，与 R65E-T1 基线模块判定同源同缓存）。

治本（栈中立、fail-closed）：算 M 的 fp 物理根时，排除满足全部条件的外部根 r：
  ① r 是既有基线模块（目录带构建清单，R65E-T1 原判据）；
  ② r 的【全部】证据文件均为基线既有文件（in base）或 manifest（R65E-T1 原案的超集）；
  ③ 剔除后 M 仍保有自有锚根（_own 非空，纯接线模块仍打回）。
保护不破：CREATE 新文件进外部模块（文件不在 base）→ 根保留仍歧义；两个新落点（round62
alarm-api）仍歧义；无 project_path 保守严判。
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
<project><modelVersion>4.0.0</modelVersion><artifactId>x</artifactId></project>
"""
_SHIRO = "ruoyi-framework/src/main/java/com/ruoyi/framework/config/ShiroConfig.java"


def _st(sid, create_files=None, writable=None):
    sc = FileScope(writable=list(writable or []), readable=[],
                   create_files=list(create_files or []))
    return SubTask(id=sid, description=sid, difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT, scope=sc)


def _plan(subtasks, modules):
    p = TaskPlan(subtasks=subtasks, parallel_groups=[[s.id for s in subtasks]])
    p.shared_contract = {"dependencies": [{"module": m} for m in modules]}
    return p


def _baseline_with_framework(tmp_path: Path, *, with_admin: bool = True) -> str:
    """既有 ruoyi-framework 模块（带 pom + 既有 ShiroConfig.java）的基线树。"""
    shiro = tmp_path / _SHIRO
    shiro.parent.mkdir(parents=True)
    shiro.write_text("public class ShiroConfig {}", encoding="utf-8")
    (tmp_path / "ruoyi-framework" / "pom.xml").write_text(_POM, encoding="utf-8")
    if with_admin:
        (tmp_path / "ruoyi-admin").mkdir()
        (tmp_path / "ruoyi-admin" / "pom.xml").write_text(_POM, encoding="utf-8")
    return str(tmp_path)


_ADMIN_CTRL = "ruoyi-admin/src/main/java/com/ruoyi/web/controller/AlarmKeyController.java"


def _fp_admin_modifies_shiro():
    return [
        {"module": "ruoyi-admin", "path": _ADMIN_CTRL},
        {"module": "ruoyi-admin", "path": _SHIRO},
    ]


# ── ① round65e14 死因本体：MODIFY 既有外部源码文件不得使模块歧义、G1 应放行 ──

def test_foreign_existing_source_modify_does_not_make_module_ambiguous(tmp_path):
    base = _baseline_with_framework(tmp_path)
    plan = _plan(
        [_st("st-biz", create_files=[_ADMIN_CTRL]),
         _st("st-wire", writable=[_SHIRO])],   # 注册认证 filter 进既有 Shiro 链
        ["ruoyi-admin"],
    )
    fp = _fp_admin_modifies_shiro()
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-admin" not in ambiguous, \
        f"MODIFY 既有外部源码文件（合法 fan-in 接线）不得使模块歧义（round65e14 死因）: {ambiguous}"
    # 猎手 Finding2：豁免后模块必须仍在 resolved（不许从 resolved/ambiguous 双消失成 zero-dir）
    assert resolved.get("ruoyi-admin") == "ruoyi-admin", \
        f"豁免后模块须解析到自有根，绝不静默消失: {resolved}"
    r = validate_module_coherence(plan, project_path=base, file_plan=fp)
    assert r.valid, f"G1 应放行（单体 Shiro 认证接线合法）: {r.issues}"


# ── ①b 猎手 Finding1（CONFIRMED HIGH）：契约标签≠物理目录字面名 + MODIFY 外部既有文件 ──
# 契约模块 `alarm-admin` 实住 `ruoyi-admin/`（R58-1 真实场景：标签与目录不同名 → 名字匹配
# 通道无法兜底 out）。旧实现：prefix 在剔除 foreign 前算（跨 top 段→None）→ 豁免清了
# ambiguous 后模块从 resolved/ambiguous 双双消失 → G1 zero-dir 误诊"幻影依赖"软 warn →
# 脚手架拿不到根 → 执行期 reactor 死型静默复活。

def test_label_mismatch_module_still_resolves_after_exemption(tmp_path):
    base = _baseline_with_framework(tmp_path)
    plan = _plan(
        [_st("st-biz", create_files=[_ADMIN_CTRL]),
         _st("st-wire", writable=[_SHIRO])],
        ["alarm-admin"],   # 标签 ≠ 目录名 ruoyi-admin
    )
    fp = [
        {"module": "alarm-admin", "path": _ADMIN_CTRL},
        {"module": "alarm-admin", "path": _SHIRO},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "alarm-admin" not in ambiguous, f"豁免应生效: {ambiguous}"
    assert resolved.get("alarm-admin") == "ruoyi-admin", \
        (f"标签≠目录名时豁免后必须用自有根重算 prefix，绝不静默消失成 zero-dir"
         f"（猎手 CONFIRMED HIGH）: {resolved}")
    r = validate_module_coherence(plan, project_path=base, file_plan=fp)
    assert r.valid, f"G1 应放行: {r.issues}"
    # zero-dir 误诊警告不得出现（模块已 resolved，不是幻影依赖）
    assert not any("幻影" in str(w) for w in (r.warnings or [])), \
        f"已 resolved 的模块不得被误诊为幻影依赖: {r.warnings}"


# ── ② 保护：CREATE【新】源码文件进既有外部模块 → 文件不在基线 → 仍歧义 ──

def test_create_new_source_in_foreign_module_still_ambiguous(tmp_path):
    base = _baseline_with_framework(tmp_path)
    new_svc = "ruoyi-framework/src/main/java/com/ruoyi/framework/web/service/TwoFactorAuthService.java"
    plan = _plan(
        [_st("st-biz", create_files=[_ADMIN_CTRL]),
         _st("st-new", create_files=[new_svc])],   # 新建文件——它的家该是 framework，不是 admin
        ["ruoyi-admin"],
    )
    fp = [
        {"module": "ruoyi-admin", "path": _ADMIN_CTRL},
        {"module": "ruoyi-admin", "path": new_svc},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-admin" in ambiguous, \
        f"CREATE 新源码进外部模块=真跨模块 smell，应保持歧义: resolved={resolved} amb={ambiguous}"


# ── ③ 保护：同一外部根下 MODIFY 既有 + CREATE 新文件混合 → 仍歧义（all() 判据）──

def test_mixed_modify_and_create_in_foreign_module_still_ambiguous(tmp_path):
    base = _baseline_with_framework(tmp_path)
    new_svc = "ruoyi-framework/src/main/java/com/ruoyi/framework/web/service/AppKeyAuthFilter.java"
    plan = _plan([_st("st-biz", create_files=[_ADMIN_CTRL])], ["ruoyi-admin"])
    fp = [
        {"module": "ruoyi-admin", "path": _ADMIN_CTRL},
        {"module": "ruoyi-admin", "path": _SHIRO},      # 既有 → 单独可豁免
        {"module": "ruoyi-admin", "path": new_svc},     # 新建 → 污染整根，不可豁免
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-admin" in ambiguous, \
        f"外部根含任一新建文件即不可豁免（新文件的家无法判）: {ambiguous}"


# ── ④ 保护：目标目录不是既有基线模块（无构建清单）→ 仍歧义（round62 保护不破）──

def test_foreign_dir_without_manifest_still_ambiguous(tmp_path):
    # ShiroConfig "存在"但 ruoyi-framework 没有构建清单 → 非既有基线模块 → 不豁免
    shiro = tmp_path / _SHIRO
    shiro.parent.mkdir(parents=True)
    shiro.write_text("public class ShiroConfig {}", encoding="utf-8")
    (tmp_path / "ruoyi-admin").mkdir()
    (tmp_path / "ruoyi-admin" / "pom.xml").write_text(_POM, encoding="utf-8")
    plan = _plan([_st("st-biz", create_files=[_ADMIN_CTRL])], ["ruoyi-admin"])
    fp = _fp_admin_modifies_shiro()
    resolved, ambiguous, collision = _resolve_module_dirs(plan, str(tmp_path), fp)
    assert "ruoyi-admin" in ambiguous, \
        f"目标目录非既有基线模块（无清单）→ 不豁免: {ambiguous}"


# ── ⑤ 保护：无 project_path → 无法证实基线存在性 → 保守严判（fail-closed）──

def test_no_project_path_stays_strict():
    plan = _plan([_st("st-biz", create_files=[_ADMIN_CTRL])], ["ruoyi-admin"])
    fp = _fp_admin_modifies_shiro()
    resolved, ambiguous, collision = _resolve_module_dirs(plan, None, fp)
    assert "ruoyi-admin" in ambiguous, \
        f"无 project_path 无法证实既有基线 → 保守严判: {ambiguous}"


# ── ⑥ 保护：纯接线模块（只改外部既有文件、无自有锚根）→ 仍歧义（_own 守卫）──

def test_pure_wiring_module_without_own_root_still_ambiguous(tmp_path):
    base = _baseline_with_framework(tmp_path)
    other = tmp_path / "ruoyi-system" / "src" / "main" / "java" / "com" / "ruoyi" / "system" / "S.java"
    other.parent.mkdir(parents=True)
    other.write_text("public class S {}", encoding="utf-8")
    (tmp_path / "ruoyi-system" / "pom.xml").write_text(_POM, encoding="utf-8")
    plan = _plan([_st("st-wire", writable=[_SHIRO])], ["ghost-mod"])
    fp = [
        {"module": "ghost-mod", "path": _SHIRO},
        {"module": "ghost-mod",
         "path": "ruoyi-system/src/main/java/com/ruoyi/system/S.java"},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ghost-mod" in ambiguous, \
        f"纯接线模块（全部根都是外部既有）无自有锚根，哪是它的家无法判 → 仍歧义: {ambiguous}"


# ── ⑦ R65E-T1 原案回归：manifest 豁免行为不变（超集不回归子集）──

def test_r65e_t1_manifest_exemption_unchanged(tmp_path):
    base = _baseline_with_framework(tmp_path)
    plan = _plan(
        [_st("st-biz", create_files=["ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"]),
         _st("st-wire", writable=["ruoyi-admin/pom.xml"])],
        ["ruoyi-alarm"],
    )
    fp = [
        {"module": "ruoyi-alarm", "path": "ruoyi-alarm/src/main/java/com/ruoyi/alarm/A.java"},
        {"module": "ruoyi-alarm", "path": "ruoyi-admin/pom.xml"},
    ]
    resolved, ambiguous, collision = _resolve_module_dirs(plan, base, fp)
    assert "ruoyi-alarm" not in ambiguous, f"R65E-T1 原案不得回归: {ambiguous}"
    assert resolved.get("ruoyi-alarm") == "ruoyi-alarm"
