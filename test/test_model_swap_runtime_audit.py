"""模型换装运行态审计：网关、路由与能力库必须形成完整证据闭环。"""

from __future__ import annotations

import json
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from swarm.models.prober import InventorySnapshot


def _config(*, active=("live",), pool=(), assignments=None):
    assignments = assignments or {name: "local" for name in (*active, *pool)}
    providers = [
        SimpleNamespace(id="local", kind="local"),
        SimpleNamespace(id="cloud", kind="cloud"),
    ]
    by_id = {p.id: p for p in providers}
    model = SimpleNamespace(
        models_in_use=lambda: list(active),
        provider_for_model=lambda name: by_id.get(assignments.get(name)),
        _effective_providers=lambda: list(providers),
    )
    worker = SimpleNamespace(worker_parallel_pool=list(pool))
    return SimpleNamespace(model=model, worker=worker)


def _snap(provider_id, *models, complete=True, error=None):
    return InventorySnapshot(
        provider_id=provider_id,
        models=tuple({"id": model} for model in models),
        model_ids=tuple(models),
        complete=complete,
        error=error,
        endpoint_kind="openai",
    )


def _audit(**kwargs):
    from swarm.models.model_inventory_audit import audit_model_inventory

    snapshots = kwargs.pop("snapshots")
    capabilities = kwargs.pop("capabilities", [])
    return audit_model_inventory(
        kwargs.pop("config", _config()),
        inventory_loader=lambda provider: snapshots[provider.id],
        capability_loader=lambda: capabilities,
        **kwargs,
    )


def test_new_model_must_exist_in_real_provider_inventory():
    report = _audit(
        snapshots={"local": _snap("local", "live"), "cloud": _snap("cloud", "c1")},
        new_model="does-not-exist",
    )
    assert report.exit_code == 1
    assert "new_model_not_found" in {v["kind"] for v in report.violations}


def test_retired_model_in_gateway_or_capability_db_is_a_violation():
    report = _audit(
        snapshots={
            "local": _snap("local", "live", "retired-online"),
            "cloud": _snap("cloud", "c1"),
        },
        capabilities=[
            {"provider_id": "cloud", "model_id": "retired-db", "source": "probed"}
        ],
        retired_models=("retired-online", "retired-db"),
    )
    kinds = {v["kind"] for v in report.violations}
    assert {"retired_model_online", "retired_capability_row"} <= kinds
    assert report.exit_code == 1


def test_retired_model_seen_in_partial_inventory_is_still_a_violation():
    report = _audit(
        snapshots={
            "local": _snap(
                "local", "live", "retired-visible",
                complete=False, error="pagination_incomplete",
            ),
            "cloud": _snap("cloud", "c1"),
        },
        retired_models=("retired-visible",),
    )

    assert report.complete is False
    assert report.exit_code == 1
    assert {
        item["kind"] for item in report.violations
    } >= {"retired_model_online"}


def test_active_model_requires_inventory_from_its_assigned_provider():
    report = _audit(
        config=_config(assignments={"live": "local"}),
        snapshots={
            "local": _snap("local", "another"),
            "cloud": _snap("cloud", "live"),
        },
    )
    assert "active_model_missing" in {v["kind"] for v in report.violations}


def test_incomplete_inventory_or_db_error_returns_unverified_code_2():
    report = _audit(
        snapshots={
            "local": _snap("local", complete=False, error="timeout"),
            "cloud": _snap("cloud", "c1"),
        },
    )
    assert report.exit_code == 2
    assert report.complete is False
    assert report.verification_errors[0]["kind"] == "inventory_unavailable"


def test_inventory_snapshot_must_match_requested_provider():
    report = _audit(
        snapshots={
            "local": _snap("cloud", "live"),
            "cloud": _snap("cloud", "c1"),
        },
    )

    assert report.exit_code == 2
    assert {
        (item["kind"], item["provider_id"])
        for item in report.verification_errors
    } == {("inventory_provider_mismatch", "local")}
    assert all(
        item["provider_id"] != "local" for item in report.provider_inventory
    )


def test_worker_pool_only_model_is_part_of_active_assignments():
    report = _audit(
        config=_config(active=("live",), pool=("pool-only",)),
        snapshots={
            "local": _snap("local", "live"),
            "cloud": _snap("cloud", "c1"),
        },
    )
    missing = [
        v for v in report.violations
        if v["kind"] == "active_model_missing"
    ]
    assert missing == [{
        "kind": "active_model_missing",
        "provider_id": "local",
        "model_id": "pool-only",
    }]


def test_same_provider_capability_absent_from_inventory_is_stale():
    report = _audit(
        snapshots={"local": _snap("local", "live"), "cloud": _snap("cloud", "c1")},
        capabilities=[
            {"provider_id": "local", "model_id": "old", "source": "manual"},
            {"provider_id": "orphan", "model_id": "ghost", "source": "probed"},
        ],
    )
    assert report.exit_code == 1
    assert {row["model_id"] for row in report.stale_capabilities} == {"old", "ghost"}


def test_serialized_report_contains_no_endpoint_or_secret_fields():
    report = _audit(
        snapshots={
            "local": _snap(
                "local",
                complete=False,
                error="GET https://admin:secret@models.invalid/v1?key=sk-secret timed out",
            ),
            "cloud": _snap("cloud", "c1"),
        },
    )
    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    assert "base_url" not in payload
    assert "api_key" not in payload
    assert "models.invalid" not in payload
    assert "secret" not in payload


def test_model_swap_cli_exit_code_consumes_runtime_report(monkeypatch, capsys):
    from swarm.models.model_inventory_audit import ModelInventoryAuditReport

    script = Path(__file__).resolve().parents[1] / "scripts" / "model_swap_audit.py"
    spec = importlib.util.spec_from_file_location("model_swap_audit_under_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    report = ModelInventoryAuditReport(
        active_assignments=(),
        provider_inventory=(),
        stale_capabilities=(),
        violations=({
            "kind": "active_model_missing",
            "provider_id": "local",
            "model_id": "missing",
        },),
        verification_errors=(),
    )
    monkeypatch.setattr(module, "_live_routing_snapshot", lambda: (set(), True))
    monkeypatch.setattr(module, "_runtime_inventory_audit", lambda *a, **k: report)
    monkeypatch.setattr(sys, "argv", [str(script)])
    previous_cwd = os.getcwd()
    try:
        assert module.main() == 1
    finally:
        os.chdir(previous_cwd)

    output = capsys.readouterr().out
    assert '"kind": "active_model_missing"' in output
    assert "无残留、无漂移" not in output


def test_model_swap_cli_never_echoes_matching_config_line(monkeypatch, capsys):
    from swarm.models.model_inventory_audit import ModelInventoryAuditReport

    script = Path(__file__).resolve().parents[1] / "scripts" / "model_swap_audit.py"
    spec = importlib.util.spec_from_file_location("model_swap_audit_redaction_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    clean = ModelInventoryAuditReport((), (), (), (), ())
    secret_line = (
        'SWARM_MODEL_PROVIDERS=[{"model":"retired-model",'
        '"base_url":"https://private.invalid/v1","api_key":"sk-secret"}]'
    )
    monkeypatch.setattr(module, "AUTHORITATIVE", [])
    monkeypatch.setattr(module, "_live_routing_snapshot", lambda: (set(), True))
    monkeypatch.setattr(
        module,
        "_iter_repo_hits",
        lambda *args, **kwargs: iter([(".env", 7, secret_line)]),
    )
    monkeypatch.setattr(module, "_runtime_inventory_audit", lambda *a, **k: clean)
    monkeypatch.setattr(sys, "argv", [str(script), "--retired", "retired-model"])
    previous_cwd = os.getcwd()
    try:
        assert module.main() == 1
    finally:
        os.chdir(previous_cwd)

    output = capsys.readouterr().out
    assert ".env:7" in output
    assert "private.invalid" not in output
    assert "sk-secret" not in output


def test_repo_scan_skips_historical_test_json_but_keeps_live_json(
    monkeypatch, tmp_path,
):
    script = Path(__file__).resolve().parents[1] / "scripts" / "model_swap_audit.py"
    spec = importlib.util.spec_from_file_location("model_swap_audit_scan_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    fixture_dir = tmp_path / "test" / "fixtures"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "history.json").write_text(
        '{"model":"retired-model"}', encoding="utf-8"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "live.json").write_text(
        '{"model":"retired-model"}', encoding="utf-8"
    )
    monkeypatch.setattr(module, "_PKG", str(tmp_path))

    hits = list(module._iter_repo_hits("retired-model"))

    assert [(rel, line) for rel, line, _ in hits] == [
        (os.path.join("config", "live.json"), 1)
    ]
