"""Finding 1（round29，2026-07-06）：worker 撞【模板悬空/过期】反向作废项目沙箱指纹。

背景：CubeSandbox 升级后旧模板节点侧 stale（130409），但 CubeMaster /templates 列表级
status 仍报 READY → template_exists_in_cubemaster 存在性探活误判可用、preprocess 误复用不重建。
唯一 ground-truth 信号是 worker 创建沙箱时的报错。这里验证：
  - error_indicates_stale_template 正确识别 130404/130409/stale/needs-redo，且不误伤普通错误；
  - invalidate_project_template_on_stale 只在 stale 错误 + 有既有指纹时清 **sandbox_deps_hash**
    （保留 sandbox_template 指针），令下次 preprocess 走重建分支；幂等、非 stale 不动。
"""
from __future__ import annotations

import swarm.worker.image_builder as ib


def test_error_marker_matches_stale_and_missing():
    # 130409 stale/needs-redo（升级后）
    assert ib.error_indicates_stale_template(
        "500: CubeMaster returned error code 130409: template tpl-x is not ready "
        "on any healthy node: template tpl-x is stale on nodes [1.2.3.4] and needs redo"
    )
    # 130404 template not found（TTL/清理回收）
    assert ib.error_indicates_stale_template("130404: template not found")
    assert ib.error_indicates_stale_template("template_not_found for tpl-y")


def test_error_marker_ignores_unrelated_errors():
    assert not ib.error_indicates_stale_template("")
    assert not ib.error_indicates_stale_template("connection refused")
    assert not ib.error_indicates_stale_template("500: internal server error")
    # 编译失败等业务错误不得被当作模板 stale
    assert not ib.error_indicates_stale_template("cannot find symbol: class Foo")


def _patch_store(monkeypatch, config):
    """把 image_builder 里惰性 import 的 project.store get/update 打桩，捕获写入。"""
    state = {"config": dict(config), "updated": None}

    def fake_get_project(pid):
        return {"id": pid, "config": dict(state["config"])}

    def fake_update_project(pid, *, config=None, **kw):
        if config is not None:
            state["config"] = dict(config)
            state["updated"] = dict(config)
        return {"id": pid, "config": dict(state["config"])}

    import swarm.project.store as store
    monkeypatch.setattr(store, "get_project", fake_get_project)
    monkeypatch.setattr(store, "update_project", fake_update_project)
    return state


def test_invalidate_clears_only_deps_hash_on_stale(monkeypatch):
    state = _patch_store(monkeypatch, {
        "sandbox_template": "tpl-abc",
        "sandbox_deps_hash": "v5-deps-src",
        "other_key": "keep-me",
    })
    changed = ib.invalidate_project_template_on_stale(
        "proj-1", "130409 ... is stale ... needs redo")
    assert changed is True
    # 指纹被清空（→ 下次 preprocess 失配走重建），指针保留（不改本次在飞行为），其它键不动
    assert state["updated"]["sandbox_deps_hash"] == ""
    assert state["updated"]["sandbox_template"] == "tpl-abc"
    assert state["updated"]["other_key"] == "keep-me"


def test_invalidate_noop_when_not_stale(monkeypatch):
    state = _patch_store(monkeypatch, {
        "sandbox_template": "tpl-abc", "sandbox_deps_hash": "v5-deps-src"})
    assert ib.invalidate_project_template_on_stale("proj-1", "connection refused") is False
    assert state["updated"] is None  # 未写库


def test_invalidate_noop_without_project_id(monkeypatch):
    state = _patch_store(monkeypatch, {"sandbox_deps_hash": "v5-x"})
    assert ib.invalidate_project_template_on_stale(None, "130409 stale needs redo") is False
    assert state["updated"] is None


def test_invalidate_idempotent_when_no_existing_hash(monkeypatch):
    # 指纹本就空（已被清/从未建）→ 无可作废，幂等返回 False，不写库
    state = _patch_store(monkeypatch, {"sandbox_template": "tpl-abc", "sandbox_deps_hash": ""})
    assert ib.invalidate_project_template_on_stale("proj-1", "130404 template not found") is False
    assert state["updated"] is None
