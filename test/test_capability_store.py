#!/usr/bin/env python3
"""模型能力库单测（设计 v3 A批1）。

两类：
  1. 启发式默认 —— 纯函数，无 DB，任何环境都跑（含 CI lint 前快测）。
  2. CRUD round-trip —— 接真 PG；连不上则 skip（本地无库不阻塞，CI 有 pgvector 库会跑）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

from swarm.models import capability_store as cap


# ── 启发式默认（纯逻辑，无 DB）────────────────────────────────

def test_heuristic_context_local_small():
    # 本地小模型（<20B）→ 32k 保守兜底
    assert cap.heuristic_context_window("qwen3:7b", kind="local") == 32_000
    print("  ✅ 启发式: 本地小模型 7b → 32k")


def test_heuristic_context_local_large():
    # 本地大模型（无名字线索、规模未知或 >=20B）→ 128k
    assert cap.heuristic_context_window("some-local-30b", kind="local") == 128_000
    print("  ✅ 启发式: 本地大模型 30b → 128k")


def test_heuristic_context_name_hint_wins():
    # 名字线索表优先于规模启发式
    assert cap.heuristic_context_window("claude-4-sonnet", kind="cloud") == 200_000
    assert cap.heuristic_context_window("gpt-4o-mini", kind="cloud") == 128_000
    print("  ✅ 启发式: 名字线索表优先 (claude-4→200k, gpt-4o→128k)")


def test_heuristic_context_cloud_default():
    assert cap.heuristic_context_window("unknown-cloud-model", kind="cloud") == 128_000
    print("  ✅ 启发式: 未知云端模型 → 128k 默认")


def test_heuristic_multimodal():
    assert cap.heuristic_supports_multimodal("some-model-NVFP4-multimodal") is True
    assert cap.heuristic_supports_multimodal("some-vl-model") is True
    assert cap.heuristic_supports_multimodal("Step-3.7-Flash") is True
    # ThinkingCap-Qwen3.6-27B 含视觉但名字无 vl/vision 线索 → 显式登记 hint(2026-07-15 换装)
    assert cap.heuristic_supports_multimodal("ThinkingCap-Qwen3.6-27B") is True
    assert cap.heuristic_supports_multimodal("plain-text-model") is False
    print("  ✅ 启发式: 多模态名字线索 (vl/multimodal/step-3/thinkingcap → True)")


def test_heuristic_thinkingcap_context_probed_137600():
    # ThinkingCap 2026-07-17 真探测 max_model_len=137600（标称 256K 不实，高估会超包 400），
    # 且须先于 "qwen3"(128K)泛匹配命中(名字含 qwen3.6)
    assert cap.heuristic_context_window("ThinkingCap-Qwen3.6-27B", kind="local") == 137_600


def test_heuristic_qwen3_coder_next_context_256k():
    # R65E-PRE 换装（替代下线 Qwen3.5 系）：真探测 262144，须先于 "qwen3"(128K)泛匹配命中
    assert cap.heuristic_context_window("Qwen3-Coder-Next-FP8", kind="local") == 262_144


def test_default_capability_shape():
    d = cap.default_capability("local", "qwen3:7b", kind="local")
    assert d["source"] == cap.SOURCE_DEFAULT
    assert d["context_window"] == 32_000
    assert d["gen_speed_tps"] == 0.0
    assert d["probed_at"] is None
    assert d["kind"] == "local"
    print("  ✅ 启发式: default_capability 结构正确，source=default")


# ── CRUD round-trip（接真 PG；连不上 skip）──────────────────

def _pg_available() -> bool:
    import psycopg

    try:
        with psycopg.connect(cap._get_conn_str(), connect_timeout=3):
            return True
    except Exception:
        return False


_pg = pytest.mark.skipif(not _pg_available(), reason="PG 不可达，跳过 CRUD round-trip")

_TEST_PROVIDER = "_test_probe_provider"


@pytest.fixture()
def _clean_pg():
    """建表 + 清掉本测试 provider 的残留，测后再清。"""
    cap.ensure_tables()
    cap.delete_provider_capabilities(_TEST_PROVIDER)
    yield
    cap.delete_provider_capabilities(_TEST_PROVIDER)


@_pg
def test_upsert_and_get(_clean_pg):
    cap.upsert_capability(
        _TEST_PROVIDER, "model-a",
        context_window=64_000, supports_multimodal=True,
        gen_speed_tps=42.5, kind="local", source=cap.SOURCE_PROBED,
        note="探测拿到",
    )
    row = cap.get_capability(_TEST_PROVIDER, "model-a")
    assert row["context_window"] == 64_000
    assert row["supports_multimodal"] is True
    assert row["gen_speed_tps"] == pytest.approx(42.5)
    assert row["source"] == cap.SOURCE_PROBED
    print("  ✅ CRUD: upsert + get round-trip")


@_pg
def test_upsert_is_idempotent_update(_clean_pg):
    cap.upsert_capability(_TEST_PROVIDER, "model-b", context_window=8_000, source=cap.SOURCE_DEFAULT)
    cap.upsert_capability(_TEST_PROVIDER, "model-b", context_window=128_000, source=cap.SOURCE_PROBED)
    rows = cap.list_capabilities(_TEST_PROVIDER)
    matching = [r for r in rows if r["model_id"] == "model-b"]
    assert len(matching) == 1, "upsert 应更新而非插重复"
    assert matching[0]["context_window"] == 128_000
    assert matching[0]["source"] == cap.SOURCE_PROBED
    print("  ✅ CRUD: upsert 幂等更新（不插重复）")


@_pg
def test_invalid_source_falls_back(_clean_pg):
    cap.upsert_capability(_TEST_PROVIDER, "model-c", source="bogus")
    row = cap.get_capability(_TEST_PROVIDER, "model-c")
    assert row["source"] == cap.SOURCE_DEFAULT
    print("  ✅ CRUD: 非法 source 回退 default")


@_pg
def test_get_or_default_when_missing(_clean_pg):
    row = cap.get_capability_or_default(_TEST_PROVIDER, "never-stored", kind="cloud")
    assert row["source"] == cap.SOURCE_DEFAULT
    assert row["context_window"] == 128_000
    print("  ✅ CRUD: 缺失时 get_or_default 返回启发式默认")


@_pg
def test_delete(_clean_pg):
    cap.upsert_capability(_TEST_PROVIDER, "model-d", context_window=1000)
    assert cap.delete_capability(_TEST_PROVIDER, "model-d") is True
    assert cap.get_capability(_TEST_PROVIDER, "model-d") is None
    assert cap.delete_capability(_TEST_PROVIDER, "model-d") is False
    print("  ✅ CRUD: delete + 重复 delete 返回 False")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
