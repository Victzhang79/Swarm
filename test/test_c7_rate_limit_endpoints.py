"""C7（round22, P2·DoS）：昂贵端点无限流（原仅 KB retrieve/ingest 有）。

根因：semantic 检索/建任务/预处理/config test/models probe/worker run 等触发嵌入/Qdrant/
Brain/真实 LLM 调用，无限流 → 单攻击者可刷爆退化服务。

治本：给这些端点加 Depends(rate_limit(...))。rate_limit 返回名为 _dep 的依赖，故可在 app 路由上
校验其挂载（route-level 行为断言，非源码字符串焊死）。
"""
from __future__ import annotations

import importlib

appmod = importlib.import_module("swarm.api.app")
app = appmod.app


def _uses_rate_limit(call) -> bool:
    """rate_limit 工厂产出的依赖识别：按【模块】(最稳)或 qualname，跨 FastAPI 版本鲁棒。"""
    return call is not None and (
        getattr(call, "__module__", "") == "swarm.api.rate_limit"
        or "rate_limit" in getattr(call, "__qualname__", "")
    )


def _collect_dep_calls(dependant) -> list:
    """递归收集 dependant 树里所有依赖的 .call（装饰器级 dependencies 会被 FastAPI 解析进此树）。"""
    calls = []
    for d in getattr(dependant, "dependencies", []) or []:
        calls.append(getattr(d, "call", None))
        calls.extend(_collect_dep_calls(d))
    return calls


def _route_deps_have_rate_limit(path: str, method: str) -> bool:
    # 复核/CI 修正：不再只看版本相关的 raw route.dependencies；查【已解析的 dependant 树】(核心解析，
    # 各 FastAPI 版本都填充) + raw，按模块/qualname 匹配 → 跨版本稳定。
    for r in app.routes:
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or set()):
            for d in getattr(r, "dependencies", []) or []:
                if _uses_rate_limit(getattr(d, "dependency", None)):
                    return True
            dep = getattr(r, "dependant", None)
            if dep and any(_uses_rate_limit(c) for c in _collect_dep_calls(dep)):
                return True
    return False


def test_semantic_rate_limited():
    assert _route_deps_have_rate_limit("/api/projects/{project_id}/knowledge/semantic", "GET")


def test_task_create_rate_limited():
    assert _route_deps_have_rate_limit("/api/projects/{project_id}/tasks", "POST")


def test_preprocess_rate_limited():
    assert _route_deps_have_rate_limit("/api/projects/{project_id}/preprocess", "POST")


def test_config_test_rate_limited():
    assert _route_deps_have_rate_limit("/api/config/test", "POST")


def test_models_probe_rate_limited():
    assert _route_deps_have_rate_limit("/api/models/probe", "POST")


def test_worker_run_rate_limited():
    assert _route_deps_have_rate_limit("/api/projects/{project_id}/worker/run", "POST")


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
