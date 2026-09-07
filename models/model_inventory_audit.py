"""模型换装的只读运行态审计核心。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from swarm.models.prober import InventorySnapshot


@dataclass(frozen=True)
class ModelInventoryAuditReport:
    active_assignments: tuple[dict[str, str], ...]
    provider_inventory: tuple[dict[str, Any], ...]
    stale_capabilities: tuple[dict[str, str], ...]
    violations: tuple[dict[str, str], ...]
    verification_errors: tuple[dict[str, str], ...]

    @property
    def complete(self) -> bool:
        return not self.verification_errors

    @property
    def ok(self) -> bool:
        return self.complete and not self.violations

    @property
    def exit_code(self) -> int:
        if self.violations:
            return 1
        if self.verification_errors:
            return 2
        return 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "active_assignments": [dict(item) for item in self.active_assignments],
            "provider_inventory": [dict(item) for item in self.provider_inventory],
            "stale_capabilities": [dict(item) for item in self.stale_capabilities],
            "violations": [dict(item) for item in self.violations],
            "verification_errors": [dict(item) for item in self.verification_errors],
        }


def _active_model_names(config) -> list[str]:
    names = list(config.model.models_in_use() or [])
    names.extend(list(getattr(config.worker, "worker_parallel_pool", []) or []))
    return list(dict.fromkeys(str(name) for name in names if name))


def _inventory_error_code(error: str | None) -> str:
    text = str(error or "")
    if not text:
        return "inventory_incomplete"
    if "认证失败" in text:
        return "authentication_failed"
    if "分页" in text or "pagination" in text.lower():
        return "pagination_incomplete"
    if "为空" in text or "empty" in text.lower():
        return "inventory_empty"
    if any(token in text for token in ("JSON", "格式", "条目", "数组")):
        return "malformed_inventory"
    if text.startswith("HTTP "):
        return "inventory_http_error"
    return "inventory_request_failed"


def audit_model_inventory(
    config,
    *,
    retired_models: tuple[str, ...] = (),
    new_model: str = "",
    expect_in_use: bool = False,
    inventory_loader: Callable[[Any], InventorySnapshot],
    capability_loader: Callable[[], list[dict[str, Any]]],
) -> ModelInventoryAuditReport:
    """交叉核对路由、各 provider 完整清单与能力库缓存。"""
    providers = {
        provider.id: provider
        for provider in (config.model._effective_providers() or [])
        if getattr(provider, "id", "")
    }
    violations: list[dict[str, str]] = []
    verification_errors: list[dict[str, str]] = []
    snapshots: dict[str, InventorySnapshot] = {}
    inventory_summary: list[dict[str, Any]] = []

    for provider_id, provider in providers.items():
        try:
            snapshot = inventory_loader(provider)
        except Exception as exc:  # noqa: BLE001
            verification_errors.append({
                "kind": "inventory_unavailable",
                "provider_id": provider_id,
                "error": type(exc).__name__,
            })
            continue
        if snapshot.provider_id != provider_id:
            verification_errors.append({
                "kind": "inventory_provider_mismatch",
                "provider_id": provider_id,
                "error": "provider_id_mismatch",
            })
            continue
        safe_error = _inventory_error_code(snapshot.error) if not snapshot.complete else ""
        snapshots[provider_id] = snapshot
        inventory_summary.append({
            "provider_id": provider_id,
            "complete": bool(snapshot.complete),
            "models": list(snapshot.model_ids),
            "error": safe_error,
        })
        if not snapshot.complete:
            verification_errors.append({
                "kind": "inventory_unavailable",
                "provider_id": provider_id,
                "error": safe_error,
            })

    assignments: list[dict[str, str]] = []
    active_names = _active_model_names(config)
    for model_id in active_names:
        provider = config.model.provider_for_model(model_id)
        provider_id = getattr(provider, "id", "") if provider else ""
        assignments.append({"provider_id": provider_id, "model_id": model_id})
        if not provider_id or provider_id not in providers:
            violations.append({
                "kind": "active_model_unmapped",
                "provider_id": provider_id,
                "model_id": model_id,
            })
            continue
        snapshot = snapshots.get(provider_id)
        if snapshot and snapshot.complete and model_id not in snapshot.model_ids:
            violations.append({
                "kind": "active_model_missing",
                "provider_id": provider_id,
                "model_id": model_id,
            })

    try:
        capability_rows = list(capability_loader() or [])
    except Exception as exc:  # noqa: BLE001
        capability_rows = []
        verification_errors.append({
            "kind": "capability_store_unavailable",
            "provider_id": "",
            "error": type(exc).__name__,
        })

    stale: list[dict[str, str]] = []
    for row in capability_rows:
        provider_id = str(row.get("provider_id") or "")
        model_id = str(row.get("model_id") or "")
        source = str(row.get("source") or "")
        if not provider_id or not model_id:
            violations.append({
                "kind": "malformed_capability_row",
                "provider_id": provider_id,
                "model_id": model_id,
            })
            continue
        snapshot = snapshots.get(provider_id)
        is_stale = provider_id not in providers or (
            snapshot is not None
            and snapshot.complete
            and model_id not in snapshot.model_ids
        )
        if is_stale:
            item = {
                "provider_id": provider_id,
                "model_id": model_id,
                "source": source,
            }
            stale.append(item)
            violations.append({
                "kind": "stale_capability_row",
                "provider_id": provider_id,
                "model_id": model_id,
            })

    retired = tuple(dict.fromkeys(name for name in retired_models if name))
    for model_id in retired:
        if model_id in active_names:
            violations.append({
                "kind": "retired_model_in_use",
                "provider_id": "",
                "model_id": model_id,
            })
        for provider_id, snapshot in snapshots.items():
            if model_id in snapshot.model_ids:
                violations.append({
                    "kind": "retired_model_online",
                    "provider_id": provider_id,
                    "model_id": model_id,
                })
        for row in capability_rows:
            if row.get("model_id") == model_id:
                violations.append({
                    "kind": "retired_capability_row",
                    "provider_id": str(row.get("provider_id") or ""),
                    "model_id": model_id,
                })

    if new_model:
        found = any(
            snapshot.complete and new_model in snapshot.model_ids
            for snapshot in snapshots.values()
        )
        if not found and not verification_errors:
            violations.append({
                "kind": "new_model_not_found",
                "provider_id": "",
                "model_id": new_model,
            })
        if expect_in_use and new_model not in active_names:
            violations.append({
                "kind": "new_model_not_in_use",
                "provider_id": "",
                "model_id": new_model,
            })

    def _dedupe(items: list[dict[str, str]]) -> tuple[dict[str, str], ...]:
        seen: set[tuple[tuple[str, str], ...]] = set()
        result: list[dict[str, str]] = []
        for item in items:
            key = tuple(sorted(item.items()))
            if key not in seen:
                seen.add(key)
                result.append(item)
        return tuple(result)

    return ModelInventoryAuditReport(
        active_assignments=tuple(assignments),
        provider_inventory=tuple(inventory_summary),
        stale_capabilities=tuple(stale),
        violations=_dedupe(violations),
        verification_errors=_dedupe(verification_errors),
    )
