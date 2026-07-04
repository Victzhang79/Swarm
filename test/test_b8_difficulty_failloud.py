"""B8（round22, P1）：未知 difficulty 静默降级为 medium tier。

根因：_resolve_route 的 route_map.get(difficulty, medium) 对拼写错误/新增 enum/非法值不报错，
直接 medium 路由（可能把 ultra/complex 任务发到弱模型），无告警。

治本：未知 difficulty → log WARNING + 显式记名回退（fail-loud，保留 medium 兜底不崩但可观测）。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from swarm.models import router as router_mod
from swarm.models.router import ModelRouter


def _router():
    r = ModelRouter.__new__(ModelRouter)
    cfg = MagicMock()
    cfg.routing_medium = "medium-model"; cfg.routing_medium_fallback = ["mf"]
    cfg.routing_complex = "complex-model"; cfg.routing_complex_fallback = ["cf"]
    cfg.routing_trivial = "trivial-model"; cfg.routing_trivial_fallback = ["tf"]
    r.config = cfg
    return r


def _warned_unknown(mock_logger) -> bool:
    return any("未知 difficulty" in str(c.args) for c in mock_logger.warning.call_args_list)


def test_unknown_difficulty_warns():
    r = _router()
    with patch.object(router_mod, "logger") as lg:
        primary, _ = r._resolve_route("ultra", "text")
    assert primary == "medium-model"  # 仍回退 medium 不崩
    assert _warned_unknown(lg), "未知 difficulty 必须 fail-loud 告警"


def test_known_difficulty_no_warn():
    r = _router()
    with patch.object(router_mod, "logger") as lg:
        primary, _ = r._resolve_route("complex", "text")
    assert primary == "complex-model"
    assert not _warned_unknown(lg)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
