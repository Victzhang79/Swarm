#!/usr/bin/env python3
"""30 号文批6 A-1 锁：`_base_tree_listing` 三态机读可辨 + ③f fail-closed。

被锁缺陷：「读 base 失败」（git ls-tree rc!=0 / ref 不可达 / OSError / 超时）曾伪装成
「真无 base」（None、零日志）⇒ ③f（create-vs-base shadow 的 correctness 硬底）静默
整闸关闭；planning_nodes:2577 那条作者写下的降级 WARNING 被内层 except 自吞挡成不可达
（层内自吞 = 外层永远收不到的教科书实例）。治法：失败返 `_BASE_TREE_UNREADABLE` 哨兵
（不可迭代/布尔——忘判三态的消费点当场 TypeError，fail-loud）+ WARNING + degrade 键；
③f 收到哨兵 fail-closed 打回，其余消费点显式跳过绝不伪装 greenfield。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import swarm.brain.contract_utils as cu
from swarm.brain.contract_utils import _BASE_TREE_UNREADABLE, _base_tree_listing


def _git_repo(tmp_path: Path) -> Path:
    """造真 git 仓（一次提交一个文件）。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "--allow-empty", "-m", "init"], cwd=repo, check=True, env=env)
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                    "-m", "add"], cwd=repo, check=True, env=env)
    return repo


# ─── 1-2. 真无 base（greenfield 正常路径）：None 且零留痕 ───

def test_no_project_path_returns_none_silently(caplog):
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        assert _base_tree_listing(None, "HEAD") is None
    assert not [r for r in caplog.records if "A-1" in r.message]


def test_non_git_dir_returns_none_silently(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        assert _base_tree_listing(str(tmp_path), "HEAD") is None
    assert not [r for r in caplog.records if "A-1" in r.message]


# ─── 3-4. 读失败：哨兵 + WARNING + degrade 键（三态核心锁）───

def test_empty_repo_unborn_head_returns_none(tmp_path, caplog):
    """批6 R1 reviewer MEDIUM 锁：空仓（git init 零 commit）ls-tree HEAD 必 rc=128，
    但 capture_base_commit 对空仓返 None（greenfield 语义）——unborn HEAD=真无 base，
    绝不误判哨兵把合法 greenfield 计划 fail-closed 打回。"""
    repo = tmp_path / "empty"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        assert _base_tree_listing(str(repo), None) is None
    assert not [r for r in caplog.records if "A-1" in r.message], "空仓是正常路径，零留痕"


def test_unreachable_ref_returns_sentinel_with_trace(tmp_path, caplog, monkeypatch):
    repo = _git_repo(tmp_path)
    seen: list[str] = []
    import swarm.infra.degrade as dg
    monkeypatch.setattr(dg, "record_degrade", lambda cat: seen.append(cat))
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        out = _base_tree_listing(str(repo), "0" * 40)  # 合法 hex 但不可达
    assert out is _BASE_TREE_UNREADABLE, \
        f"读失败必须返哨兵（返 None=A-1 原洞复发：伪装 greenfield 关 ③f）: {out!r}"
    assert any("A-1" in r.message and "读失败≠真无 base" in r.message for r in caplog.records), \
        "读失败必须 WARNING 留痕（原实现零日志）"
    assert "brain.base_tree.unreadable" in seen, \
        f"degrade 键必须有消费者可读的账: {seen}"


def test_oserror_returns_sentinel_with_trace(tmp_path, caplog, monkeypatch):
    (tmp_path / ".git").mkdir()  # 过 isdir 检查即可，subprocess 打炸
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("git binary gone")))
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        out = _base_tree_listing(str(tmp_path), "HEAD")
    assert out is _BASE_TREE_UNREADABLE
    assert any("A-1" in r.message for r in caplog.records)


# ─── 5. 正常路径：有效 ref → 清单，零 WARNING ───

def test_valid_ref_returns_listing(tmp_path, caplog):
    repo = _git_repo(tmp_path)
    with caplog.at_level(logging.WARNING, logger="swarm.brain.contract_utils"):
        out = _base_tree_listing(str(repo), "HEAD")
    assert isinstance(out, list) and "f.txt" in out
    assert not [r for r in caplog.records if "A-1" in r.message]


# ─── 6. 哨兵形状锁：不可迭代/不可布尔（消费点忘判三态当场炸，fail-loud）───

def test_sentinel_refuses_list_semantics():
    for op in (lambda: bool(_BASE_TREE_UNREADABLE),
               lambda: len(_BASE_TREE_UNREADABLE),
               lambda: list(_BASE_TREE_UNREADABLE),
               lambda: "x" in _BASE_TREE_UNREADABLE):
        with pytest.raises(TypeError):
            op()


# ─── 7-8. ③f correctness 硬底：收到哨兵 → fail-closed 打回（承重消费侧锁）───

def test_shadow_gate_propagates_sentinel(monkeypatch):
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: _BASE_TREE_UNREADABLE)
    from swarm.brain.plan_validator import _created_class_shadows_base
    out = _created_class_shadows_base(SimpleNamespace(subtasks=[]), "p", "r")
    assert out is _BASE_TREE_UNREADABLE, "③f 不得把读失败静默吞成 {}"


def test_validate_module_coherence_rejects_on_unreadable(monkeypatch):
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: _BASE_TREE_UNREADABLE)
    from swarm.brain.plan_validator import validate_module_coherence
    res = validate_module_coherence(SimpleNamespace(subtasks=[]), project_path="p", base_ref="r")
    assert not res.valid, "base 读失败时 ③f 必须 fail-closed 打回（仅 WARNING=仍放行，A-1 否决）"
    assert any("base 树读取失败" in i and "环境/基线" in i for i in res.issues), res.issues


def test_validate_module_coherence_greenfield_still_skips(monkeypatch):
    """反向锁：真无 base（None）→ ③f 照旧静默跳过（greenfield 不误伤，行为不变）。"""
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: None)
    from swarm.brain.plan_validator import validate_module_coherence
    res = validate_module_coherence(SimpleNamespace(subtasks=[]), project_path="p", base_ref="r")
    assert res.valid, res.issues


# ─── 9-11. 其余三消费点：哨兵显式跳过（不炸、不伪装）───

def test_cvb_deconflict_skips_on_sentinel(monkeypatch):
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: _BASE_TREE_UNREADABLE)
    plan = SimpleNamespace(subtasks=[SimpleNamespace(
        scope=SimpleNamespace(create_files=["a/X.java"], writable=[], readable=[]))])
    assert cu.deconflict_create_vs_base_modify_shadow(plan, None, "p", "r") == 0


def test_rule0_normalize_scopes_survives_sentinel(monkeypatch):
    """规则0 重定位 pass：哨兵不得从 `if _tree:` 漏成 TypeError（接线锁=不炸）。
    ★夹具必须带真子任务★——normalize_plan_scopes 对空 subtasks 早返，到不了规则0块
    （首轮本锁用 [] 写成 vacuous 绿，M6 合轮逮出）。"""
    monkeypatch.setattr(cu, "_base_tree_listing", lambda *a, **k: _BASE_TREE_UNREADABLE)
    plan = SimpleNamespace(subtasks=[SimpleNamespace(
        id="st-1",
        scope=SimpleNamespace(create_files=[], writable=["a/X.java"], readable=[]))])
    cu.normalize_plan_scopes(plan, project_path="p", base_ref="r")


def test_hints_returns_empty_on_sentinel():
    from swarm.brain.planning_nodes import _contract_base_entity_hints
    assert _contract_base_entity_hints([], "m", "p", "r", tree=_BASE_TREE_UNREADABLE) == ""


def test_contract_dep_guidance_degrades_on_sentinel():
    """批6 R1 hunter HIGH 锁：`_cd_tree` 的间接消费者——哨兵经 `tree or []` 会求布尔
    当场 TypeError，一次 git 抖动放大成 contract_design 整节点异常。退化 generic 指引。"""
    from swarm.brain.planning_nodes import _contract_dep_guidance
    # 空 stack（build 未判明）+ 哨兵树：两路证据皆无 → generic（带 build=maven 会命中
    # 专属档而非 generic——hunter 建议的夹具形状在此不精确，夹具修正为 {}）
    guidance, labels = _contract_dep_guidance({}, _BASE_TREE_UNREADABLE)
    assert labels == ["generic"], f"读失败必须退化 generic（不炸、不伪装）: {labels}"
    assert guidance, "generic 指引文本非空"
