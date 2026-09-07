"""Batch 4B-2：provider 清单与能力缓存必须按完整快照安全收敛。"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from swarm.models import capability_store as cap
from swarm.models import prober
from swarm.models.router import ModelRouter
from swarm.api.routers import config as config_api


def _provider(provider_id: str = "local", kind: str = "local"):
    return SimpleNamespace(
        id=provider_id,
        kind=kind,
        base_url="http://models.invalid/v1",
        api_key="",
        tls_insecure=False,
    )


def _record(provider_id: str, model_id: str) -> dict:
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "context_window": 8192,
        "supports_multimodal": False,
        "gen_speed_tps": 1.0,
        "kind": "local",
        "source": cap.SOURCE_PROBED,
        "note": "",
        "probed_at": None,
        "probe_complete": True,
        "incomplete_dimensions": [],
    }


def test_full_probe_reconciles_only_from_complete_inventory(monkeypatch):
    snapshot = prober.InventorySnapshot(
        provider_id="local",
        models=({"id": "live-a"}, {"id": "live-b"}),
        model_ids=("live-a", "live-b"),
        complete=True,
        error=None,
        endpoint_kind="openai",
    )
    monkeypatch.setattr(prober, "list_models_snapshot", lambda provider: snapshot)
    monkeypatch.setattr(
        prober,
        "probe_model",
        lambda provider, model_id, obj, measure_speed=True: _record(provider.id, model_id),
    )
    monkeypatch.setattr(cap, "begin_provider_reconcile", MagicMock(return_value=7))
    reconcile = MagicMock(return_value={
        "status": "applied",
        "generation": 7,
        "upserted": ["live-a", "live-b"],
        "pruned": ["retired"],
        "protected_manual": ["manual-old"],
    })
    monkeypatch.setattr(cap, "reconcile_provider_capabilities", reconcile)

    result = prober.probe_provider(
        _provider(), persist=True, reconcile_stale=True, measure_speed=False
    )

    assert result["inventory_complete"] is True
    assert result["reconcile"]["status"] == "applied"
    assert result["reconcile"]["pruned"] == ["retired"]
    reconcile.assert_called_once()
    kwargs = reconcile.call_args.kwargs
    assert kwargs["authoritative_model_ids"] == ("live-a", "live-b")
    assert kwargs["generation"] == 7
    assert kwargs["complete"] is True


def test_reconcile_generation_is_ordered_by_request_start(monkeypatch):
    older_inventory_started = threading.Event()
    release_older_inventory = threading.Event()
    generation_lock = threading.Lock()
    latest_generation = 0
    applied: list[tuple[int, str]] = []
    results: dict[str, dict] = {}

    def inventory(provider):
        name = threading.current_thread().name
        if name == "older-request":
            older_inventory_started.set()
            assert release_older_inventory.wait(timeout=3)
            model_id = "older-model"
        else:
            model_id = "newer-model"
        return prober.InventorySnapshot(
            provider_id=provider.id,
            models=({"id": model_id},),
            model_ids=(model_id,),
            complete=True,
            error=None,
            endpoint_kind="openai",
        )

    def begin(provider_id, conn_str=None):
        nonlocal latest_generation
        with generation_lock:
            latest_generation += 1
            return latest_generation

    def reconcile(provider_id, records, *, generation, **kwargs):
        with generation_lock:
            if generation != latest_generation:
                return {"status": "superseded", "generation": generation}
            applied.append((generation, records[0]["model_id"]))
            return {"status": "applied", "generation": generation}

    monkeypatch.setattr(prober, "list_models_snapshot", inventory)
    monkeypatch.setattr(cap, "begin_provider_reconcile", begin)
    monkeypatch.setattr(cap, "reconcile_provider_capabilities", reconcile)
    monkeypatch.setattr(
        prober,
        "probe_model",
        lambda provider, model_id, obj, measure_speed=True: _record(
            provider.id, model_id
        ),
    )

    def run(name):
        results[name] = prober.probe_provider(
            _provider(), persist=True, reconcile_stale=True, measure_speed=False
        )

    older = threading.Thread(target=run, args=("older",), name="older-request")
    newer = threading.Thread(target=run, args=("newer",), name="newer-request")
    older.start()
    assert older_inventory_started.wait(timeout=3)
    newer.start()
    newer.join(timeout=3)
    assert not newer.is_alive()
    release_older_inventory.set()
    older.join(timeout=3)
    assert not older.is_alive()

    assert results["newer"]["reconcile"]["status"] == "applied"
    assert results["older"]["reconcile"]["status"] == "superseded"
    assert applied == [(2, "newer-model")]


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            lambda: prober.InventorySnapshot(
                provider_id="local", models=(), model_ids=(), complete=False,
                error="bad_json", endpoint_kind="openai",
            ),
            "inventory_incomplete",
        ),
        (
            lambda: prober.InventorySnapshot(
                provider_id="local", models=(), model_ids=(), complete=True,
                error=None, endpoint_kind="openai",
            ),
            "inventory_empty",
        ),
    ],
)
def test_incomplete_or_empty_inventory_never_prunes(monkeypatch, snapshot, reason):
    monkeypatch.setattr(prober, "list_models_snapshot", lambda provider: snapshot())
    reconcile = MagicMock()
    monkeypatch.setattr(cap, "reconcile_provider_capabilities", reconcile)
    monkeypatch.setattr(cap, "begin_provider_reconcile", MagicMock(return_value=1))

    result = prober.probe_provider(
        _provider(), persist=True, reconcile_stale=True, measure_speed=False
    )

    assert result["reconcile"] == {"status": "skipped", "reason": reason}
    reconcile.assert_not_called()


def test_snapshot_from_another_provider_never_probes_or_reconciles(monkeypatch):
    snapshot = prober.InventorySnapshot(
        provider_id="other",
        models=({"id": "live"},),
        model_ids=("live",),
        complete=True,
        error=None,
        endpoint_kind="openai",
    )
    monkeypatch.setattr(prober, "list_models_snapshot", lambda provider: snapshot)
    probe_model = MagicMock(return_value=_record("local", "live"))
    begin = MagicMock(return_value=1)
    reconcile = MagicMock()
    monkeypatch.setattr(prober, "probe_model", probe_model)
    monkeypatch.setattr(cap, "begin_provider_reconcile", begin)
    monkeypatch.setattr(cap, "reconcile_provider_capabilities", reconcile)

    result = prober.probe_provider(
        _provider(), persist=True, reconcile_stale=True, measure_speed=False
    )

    assert result["inventory_complete"] is False
    assert result["error"] == "模型清单 provider 身份不匹配"
    assert result["reconcile"] == {
        "status": "skipped",
        "reason": "provider_mismatch",
    }
    probe_model.assert_not_called()
    begin.assert_called_once_with("local", conn_str=None)
    reconcile.assert_not_called()


def test_partial_probe_or_model_error_never_prunes(monkeypatch):
    snapshot = prober.InventorySnapshot(
        provider_id="local",
        models=({"id": "live"},),
        model_ids=("live",),
        complete=True,
        error=None,
        endpoint_kind="openai",
    )
    monkeypatch.setattr(prober, "list_models_snapshot", lambda provider: snapshot)
    monkeypatch.setattr(
        prober, "probe_model", MagicMock(side_effect=RuntimeError("probe failed"))
    )
    monkeypatch.setattr(cap, "begin_provider_reconcile", MagicMock(return_value=2))
    reconcile = MagicMock()
    monkeypatch.setattr(cap, "reconcile_provider_capabilities", reconcile)

    result = prober.probe_provider(
        _provider(), persist=True, reconcile_stale=True, measure_speed=False
    )
    assert result["reconcile"] == {"status": "skipped", "reason": "model_errors"}
    reconcile.assert_not_called()

    partial = prober.probe_provider(
        _provider(), only_models=["live"], persist=True,
        reconcile_stale=True, measure_speed=False,
    )
    assert partial["reconcile"] == {"status": "skipped", "reason": "partial_scope"}
    reconcile.assert_not_called()


def test_regular_probe_marks_upsert_to_preserve_manual(monkeypatch):
    snapshot = prober.InventorySnapshot(
        provider_id="local",
        models=({"id": "live"},),
        model_ids=("live",),
        complete=True,
        error=None,
        endpoint_kind="openai",
    )
    monkeypatch.setattr(prober, "list_models_snapshot", lambda provider: snapshot)
    monkeypatch.setattr(
        prober,
        "probe_model",
        lambda provider, model_id, obj, measure_speed=True: _record(provider.id, model_id),
    )
    upsert = MagicMock(return_value=_record("local", "live"))
    monkeypatch.setattr(cap, "upsert_capability", upsert)

    result = prober.probe_provider(
        _provider(), persist=True, reconcile_stale=False, measure_speed=False
    )

    assert result["probed"] == 1
    assert upsert.call_args.kwargs["preserve_manual"] is True


def test_api_in_use_scope_includes_worker_parallel_pool_only_models():
    local = _provider()
    model = MagicMock()
    model.models_in_use_for_provider.return_value = ["routed"]
    model.provider_for_model.side_effect = lambda name: local
    cfg = SimpleNamespace(
        model=model,
        worker=SimpleNamespace(worker_parallel_pool=["pool-only", "routed"]),
    )

    assert config_api._models_in_use_for_provider(cfg, "local") == [
        "routed", "pool-only"
    ]


@pytest.mark.asyncio
async def test_models_api_does_not_echo_inventory_exception_details(monkeypatch):
    cfg = SimpleNamespace(
        model=SimpleNamespace(_effective_providers=lambda: [_provider()]),
    )
    monkeypatch.setattr(config_api, "_require_user", lambda request: object())
    monkeypatch.setattr(config_api._app, "get_config", lambda: cfg)
    monkeypatch.setattr(
        prober,
        "list_models_snapshot",
        MagicMock(side_effect=RuntimeError(
            "GET https://admin:secret@models.invalid/v1?key=sk-secret"
        )),
    )

    result = await config_api.list_models(MagicMock())

    error = result["by_provider"]["local"]["error"]
    assert error == "模型列表读取失败（RuntimeError）"
    assert "models.invalid" not in str(result)
    assert "secret" not in str(result)


def test_stale_classifier_is_provider_scoped_and_protects_manual_unknown():
    rows = [
        {"provider_id": "local", "model_id": "live", "source": cap.SOURCE_PROBED},
        {"provider_id": "local", "model_id": "old-p", "source": cap.SOURCE_PROBED},
        {"provider_id": "local", "model_id": "old-d", "source": cap.SOURCE_DEFAULT},
        {"provider_id": "local", "model_id": "old-m", "source": cap.SOURCE_MANUAL},
        {"provider_id": "local", "model_id": "old-x", "source": "future-source"},
        {"provider_id": "other", "model_id": "old-p", "source": cap.SOURCE_PROBED},
    ]

    classified = cap.classify_stale_capabilities("local", rows, {"live"})

    assert classified == {
        "prunable": ["old-d", "old-p"],
        "protected_manual": ["old-m"],
        "protected_unknown": ["old-x"],
    }


def test_reconcile_boundary_rejects_incomplete_probe_record(monkeypatch):
    record = _record("local", "live")
    record["probe_complete"] = False
    get_conn = MagicMock(side_effect=AssertionError("不应进入数据库事务"))
    monkeypatch.setattr(cap, "_get_conn", get_conn)

    result = cap.reconcile_provider_capabilities(
        "local",
        [record],
        authoritative_model_ids=("live",),
        complete=True,
        generation=1,
    )

    assert result == {"status": "skipped", "reason": "probe_incomplete"}
    get_conn.assert_not_called()


def _router_config():
    local = _provider("local", "local")
    cloud = _provider("cloud", "cloud")
    cfg = MagicMock()
    cfg.provider_for_model.side_effect = lambda name: local
    cfg.routing_trivial = "same-name"
    cfg.routing_trivial_fallback = []
    cfg.routing_medium = "same-name"
    cfg.routing_medium_fallback = []
    cfg.routing_complex = "same-name"
    cfg.routing_complex_fallback = []
    cfg.routing_multimodal = "same-name"
    cfg.routing_multimodal_fallback = []
    cfg.brain_primary = "same-name"
    cfg.brain_fallback = "same-name"
    return cfg, local, cloud


def test_router_reachability_requires_matching_provider_row(monkeypatch):
    cfg, _, _ = _router_config()
    router = ModelRouter.__new__(ModelRouter)
    router.config = cfg
    monkeypatch.setattr(
        cap,
        "list_capabilities",
        lambda: [
            {
                "provider_id": "cloud",
                "model_id": "same-name",
                "source": cap.SOURCE_PROBED,
            },
            {
                "provider_id": "local",
                "model_id": "another-local-model",
                "source": cap.SOURCE_PROBED,
            },
        ],
    )
    # 清单已原子收敛 → 模型缺席=被穷举证明 → 硬不可达（ERROR 级）
    monkeypatch.setattr(
        cap, "latest_applied_generations", lambda: {"cloud": 1, "local": 1}
    )

    issues = router.validate_routing_reachability()

    assert issues
    assert all(issue["kind"] == "whole_chain_unreachable" for issue in issues)


def test_cloud_reachability_uses_its_own_provider_evidence(monkeypatch):
    cfg, _, cloud = _router_config()
    cfg.provider_for_model.side_effect = lambda name: cloud
    router = ModelRouter.__new__(ModelRouter)
    router.config = cfg
    monkeypatch.setattr(
        cap,
        "list_capabilities",
        lambda: [{
            "provider_id": "cloud",
            "model_id": "another-cloud-model",
            "source": cap.SOURCE_PROBED,
        }],
    )
    monkeypatch.setattr(cap, "latest_applied_generations", lambda: {"cloud": 1})

    issues = router.validate_routing_reachability()

    assert issues
    assert all(issue["kind"] == "whole_chain_unreachable" for issue in issues)


def test_reachability_unproven_inventory_only_warns(monkeypatch):
    """换装窗口期：provider 有探测行但清单【从未原子收敛】→ 缺席不算证明，
    全链不可达只降 WARNING（whole_chain_unproven），不刷 ERROR。"""
    cfg, _, cloud = _router_config()
    cfg.provider_for_model.side_effect = lambda name: cloud
    router = ModelRouter.__new__(ModelRouter)
    router.config = cfg
    monkeypatch.setattr(
        cap,
        "list_capabilities",
        lambda: [{
            "provider_id": "cloud",
            "model_id": "another-cloud-model",
            "source": cap.SOURCE_PROBED,
        }],
    )
    # applied=0 / 缺行 = 从未收敛
    monkeypatch.setattr(cap, "latest_applied_generations", lambda: {"cloud": 0})

    issues = router.validate_routing_reachability()

    assert issues
    assert all(issue["severity"] == "warning" for issue in issues)
    assert all(issue["kind"] == "whole_chain_unproven" for issue in issues)
    assert not any(i["kind"] == "whole_chain_unreachable" for i in issues)


def test_multimodal_autodiscovery_ignores_row_from_wrong_provider(monkeypatch):
    cfg, _, _ = _router_config()
    router = ModelRouter.__new__(ModelRouter)
    router.config = cfg
    monkeypatch.setattr(
        cap,
        "list_capabilities",
        lambda: [{
            "provider_id": "cloud",
            "model_id": "same-name",
            "supports_multimodal": True,
            "source": cap.SOURCE_PROBED,
            "context_window": 999999,
        }],
    )

    assert router._multimodal_model_from_capabilities() is None


_pg = pytest.mark.needs_service("pg")
_PG_PROVIDER = "_test_batch4b2_reconcile"
_PG_OTHER = "_test_batch4b2_other"


@pytest.fixture()
def _clean_reconcile_pg():
    cap.ensure_tables()
    cap.delete_provider_capabilities(_PG_PROVIDER)
    cap.delete_provider_capabilities(_PG_OTHER)
    yield
    cap.delete_provider_capabilities(_PG_PROVIDER)
    cap.delete_provider_capabilities(_PG_OTHER)


@_pg
def test_reconcile_transaction_preserves_manual_and_other_provider(_clean_reconcile_pg):
    cap.upsert_capability(
        _PG_PROVIDER, "live", context_window=777, source=cap.SOURCE_MANUAL
    )
    cap.upsert_capability(_PG_PROVIDER, "old-auto", source=cap.SOURCE_PROBED)
    cap.upsert_capability(_PG_PROVIDER, "old-manual", source=cap.SOURCE_MANUAL)
    cap.upsert_capability(_PG_OTHER, "old-auto", source=cap.SOURCE_PROBED)
    generation = cap.begin_provider_reconcile(_PG_PROVIDER)

    result = cap.reconcile_provider_capabilities(
        _PG_PROVIDER,
        [_record(_PG_PROVIDER, "live")],
        authoritative_model_ids=("live",),
        complete=True,
        generation=generation,
    )

    assert result["status"] == "applied"
    assert result["pruned"] == ["old-auto"]
    assert result["protected_manual"] == ["old-manual"]
    assert cap.get_capability(_PG_PROVIDER, "live")["context_window"] == 777
    assert cap.get_capability(_PG_PROVIDER, "live")["source"] == cap.SOURCE_MANUAL
    assert cap.get_capability(_PG_PROVIDER, "old-auto") is None
    assert cap.get_capability(_PG_PROVIDER, "old-manual") is not None
    assert cap.get_capability(_PG_OTHER, "old-auto") is not None


@_pg
def test_reconcile_preserves_live_unknown_source(_clean_reconcile_pg):
    cap.upsert_capability(
        _PG_PROVIDER, "live-future", context_window=555,
        source=cap.SOURCE_DEFAULT,
    )
    with cap._get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE model_capabilities SET source = %s "
                "WHERE provider_id = %s AND model_id = %s",
                ("future-source", _PG_PROVIDER, "live-future"),
            )
    generation = cap.begin_provider_reconcile(_PG_PROVIDER)

    result = cap.reconcile_provider_capabilities(
        _PG_PROVIDER,
        [_record(_PG_PROVIDER, "live-future")],
        authoritative_model_ids=("live-future",),
        complete=True,
        generation=generation,
    )

    saved = cap.get_capability(_PG_PROVIDER, "live-future")
    assert result["status"] == "applied"
    assert saved["source"] == "future-source"
    assert saved["context_window"] == 555


@_pg
def test_older_reconcile_generation_cannot_overwrite_newer_snapshot(_clean_reconcile_pg):
    old_generation = cap.begin_provider_reconcile(_PG_PROVIDER)
    new_generation = cap.begin_provider_reconcile(_PG_PROVIDER)
    newer = cap.reconcile_provider_capabilities(
        _PG_PROVIDER,
        [_record(_PG_PROVIDER, "new")],
        authoritative_model_ids=("new",),
        complete=True,
        generation=new_generation,
    )
    older = cap.reconcile_provider_capabilities(
        _PG_PROVIDER,
        [_record(_PG_PROVIDER, "old")],
        authoritative_model_ids=("old",),
        complete=True,
        generation=old_generation,
    )

    assert newer["status"] == "applied"
    assert older["status"] == "superseded"
    assert cap.get_capability(_PG_PROVIDER, "new") is not None
    assert cap.get_capability(_PG_PROVIDER, "old") is None


@_pg
def test_same_reconcile_generation_cannot_be_replayed_with_other_snapshot(
    _clean_reconcile_pg,
):
    generation = cap.begin_provider_reconcile(_PG_PROVIDER)
    first = cap.reconcile_provider_capabilities(
        _PG_PROVIDER,
        [_record(_PG_PROVIDER, "first")],
        authoritative_model_ids=("first",),
        complete=True,
        generation=generation,
    )
    replay = cap.reconcile_provider_capabilities(
        _PG_PROVIDER,
        [_record(_PG_PROVIDER, "replayed")],
        authoritative_model_ids=("replayed",),
        complete=True,
        generation=generation,
    )

    assert first["status"] == "applied"
    assert replay["status"] == "superseded"
    assert replay["reason"] == "generation_already_applied"
    assert cap.get_capability(_PG_PROVIDER, "first") is not None
    assert cap.get_capability(_PG_PROVIDER, "replayed") is None
