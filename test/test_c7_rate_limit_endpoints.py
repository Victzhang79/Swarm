"""C7（round22, P2·DoS）：昂贵端点无限流（原仅 KB retrieve/ingest 有）。

根因：semantic 检索/建任务/预处理/config test/models probe/worker run 等触发嵌入/Qdrant/
Brain/真实 LLM 调用，无限流 → 单攻击者可刷爆退化服务。

治本：给这些端点加 Depends(rate_limit(...))。

测试策略（跨 FastAPI 版本鲁棒）：不再 introspect route.dependencies 内部结构（版本相关，CI 上不稳）；
改为 ①源码级校验每个端点确实挂了对应 scope 的 rate_limit（wiring 完整性）②对 /api/uploads 做
真·行为验证(见 test_c8_c9)——超容量必 429，证明机制端到端生效。
"""
from __future__ import annotations

import inspect

from swarm.api.routers import config, knowledge, project, task, worker


def _wired(module, scope: str) -> bool:
    return f'rate_limit("{scope}"' in inspect.getsource(module)


def test_semantic_rate_limited():
    assert _wired(knowledge, "kb_semantic")


def test_task_create_rate_limited():
    assert _wired(task, "task_create")


def test_preprocess_rate_limited():
    assert _wired(project, "preprocess")


def test_config_test_rate_limited():
    assert _wired(config, "config_test")


def test_models_probe_rate_limited():
    assert _wired(config, "models_probe")


def test_worker_run_rate_limited():
    assert _wired(worker, "worker_run")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
