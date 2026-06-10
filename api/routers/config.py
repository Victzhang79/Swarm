"""api/routers/config.py — 配置域路由 (获取/模型列表/测试连接/更新/路由策略读写)。

从 api/app.py 抽出, app.include_router 挂载。
app 级符号(_PROJECT_ROOT/configure_langsmith/reload_config/get_config/logger)
用 _app. 属性访问保持单一定义。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request

import swarm.api.app as _app
from swarm.api._shared import _flatten_model_config, _mask_config_dict, _resolve_key

router = APIRouter()


@router.get("/api/config", tags=["配置"])
async def get_config_endpoint():
    """返回当前配置（脱敏 API Key）"""
    cfg = _app.get_config()
    raw = cfg.model_dump()
    masked = _mask_config_dict(raw)
    flat = _mask_config_dict(_flatten_model_config(cfg))
    from swarm.tracing import langsmith_status

    return {"config": masked, "flat": flat, "langsmith": langsmith_status()}


# ─── 3.5 GET /api/models ─────────────────────────
@router.get("/api/models", tags=["配置"])
async def list_models():
    """从 SiliconFlow 和本地 API 拉取可用模型列表"""
    import httpx

    result = {"siliconflow": [], "local": []}

    # SiliconFlow 模型列表
    cfg = _app.get_config()
    sf_key = cfg.model.siliconflow_api_key
    if sf_key:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{cfg.model.siliconflow_base_url}/models",
                    headers={"Authorization": f"Bearer {sf_key}"},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("data", data.get("models", []))
                    result["siliconflow"] = sorted(
                        [m.get("id", m.get("name", "")) for m in models if m.get("id") or m.get("name")]
                    )
        except Exception as e:
            _app.logger.warning(f"Failed to fetch SiliconFlow models: {e}")
            result["siliconflow_error"] = str(e)

    # 本地模型列表 (支持 OpenAI 兼容 / Open WebUI / Ollama)
    local_url = cfg.model.local_base_url
    local_key = cfg.model.local_api_key
    if local_url:
        try:
            headers = {}
            if local_key:
                headers["Authorization"] = f"Bearer {local_key}"
            base = local_url.rstrip("/").removesuffix("/v1").removesuffix("/api")
            async with httpx.AsyncClient(timeout=10, verify=False) as client:
                # 尝试多种端点：OpenAI /v1/models → Open WebUI /api/models → Ollama /api/tags
                models = []
                for endpoint in [f"{base}/v1/models", f"{base}/api/models", f"{base}/api/tags"]:
                    try:
                        resp = await client.get(endpoint, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            # OpenAI 格式: {"data": [{"id": "..."}]}
                            # Ollama 格式: {"models": [{"name": "..."}]}
                            raw = data.get("data", data.get("models", []))
                            models = [m.get("id", m.get("name", "")) for m in raw if m.get("id") or m.get("name")]
                            if models:
                                break
                        elif resp.status_code == 401:
                            result["local_error"] = "认证失败：请配置本地 API Key"
                            break
                    except Exception:
                        continue
                if models:
                    result["local"] = sorted(models)
        except Exception as e:
            _app.logger.warning(f"Failed to fetch local models: {e}")
            result["local_error"] = str(e)

    return result


@router.post("/api/config/test", tags=["配置"])
async def test_config():
    """测试 Brain / 本地 Worker / 云端 Worker 模型是否可调用"""

    cfg = _app.get_config()
    from swarm.models.router import ModelRouter

    router = ModelRouter()
    results: dict[str, Any] = {}

    def _probe(label: str, llm_factory) -> dict[str, Any]:
        try:
            llm = llm_factory()
            resp = llm.invoke([{"role": "user", "content": "Reply with exactly: OK"}])
            content = resp.content
            if isinstance(content, list):
                content = " ".join(
                    p if isinstance(p, str) else p.get("text", "")
                    for p in content
                )
            preview = str(content).strip()[:120]
            return {"ok": True, "preview": preview}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    results["brain_primary"] = {
        "model": cfg.model.brain_primary,
        **await asyncio.to_thread(
            _probe, "brain", router.get_brain_llm,
        ),
    }
    results["worker_local_medium"] = {
        "model": cfg.model.routing_medium,
        **await asyncio.to_thread(
            lambda: _probe("medium", lambda: router.get_llm_for_subtask("medium", "text")),
        ),
    }
    results["worker_cloud_complex"] = {
        "model": cfg.model.routing_complex,
        **await asyncio.to_thread(
            lambda: _probe("complex", lambda: router.get_llm_for_subtask("complex", "text")),
        ),
    }
    results["all_ok"] = all(
        results[k].get("ok") for k in (
            "brain_primary", "worker_local_medium", "worker_cloud_complex"
        )
    )
    return results


# ─── 4. PUT /api/config ────────────────────────────
@router.put("/api/config", tags=["配置"])
async def update_config(request: Request):
    """更新配置 — 写入 .env 文件并重新加载

    接收格式:
    - {"config": {"siliconflow_api_key": "...", ...}}
    - {"siliconflow_api_key": "...", ...}
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    raw_items = body.get("config", body) if isinstance(body.get("config"), dict) else body

    env_path = _app._PROJECT_ROOT / ".env"

    # 读取现有 .env 内容
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    # 构建更新映射（支持短名自动转换）
    update_map: dict[str, str] = {}
    for key, value in raw_items.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            continue
        str_val = str(value).strip()
        # 跳过空值 — 前端不输入 Key 时不应该覆盖已有 Key 为空
        if not str_val:
            continue
        # 跳过脱敏值（包含 *** 或 ... 的值是前端回传的脱敏显示值）
        if "***" in str_val or (str_val.count("...") == 1 and len(str_val) < 30):
            continue
        env_key = _resolve_key(key.strip())
        update_map[env_key] = str_val

    if not update_map:
        # 所有值都是脱敏值或未修改 — 不算错误
        cfg = _app.get_config()
        raw = cfg.model_dump()
        masked = _mask_config_dict(raw)
        return {"status": "no_changes", "message": "未检测到有效变更（脱敏值已忽略）", "config": masked}

    # 更新或追加行
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip().upper()
            if k in update_map:
                new_lines.append(f"{k}={update_map[k]}")
                updated_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    # 追加新键
    for k, v in update_map.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    # 写回 .env
    content = "\n".join(new_lines) + "\n"
    env_path.write_text(content, encoding="utf-8")

    # 同步更新 os.environ（让 _app.reload_config 新建的 BaseSettings 能读到新值）
    for k, v in update_map.items():
        os.environ[k] = v
    _app.logger.info(f"Updated .env + os.environ with keys: {list(update_map.keys())}")

    # 重新加载配置
    _app.reload_config()
    _app.configure_langsmith(reload=True)
    try:
        from swarm.worker.sandbox import reset_sandbox_manager
        reset_sandbox_manager()
    except Exception as exc:
        _app.logger.warning("Failed to reset sandbox manager after config reload: %s", exc)
    _app.logger.info("Config reloaded")

    cfg = _app.get_config()
    raw = cfg.model_dump()
    masked = _mask_config_dict(raw)

    return {
        "status": "ok",
        "updated_keys": list(update_map.keys()),
        "config": masked,
    }


# ─── 4.5 GET /api/routing ────────────────────────────
@router.get("/api/routing", tags=["配置"])
async def get_routing():
    """获取当前模型路由表"""
    from swarm.models.router import ModelRouter
    router = ModelRouter()
    return router.get_routing_table()


# ─── 4.6 PUT /api/routing ────────────────────────────
@router.put("/api/routing", tags=["配置"])
async def update_routing(request: Request):
    """更新模型路由表配置 — 写入 .env 并重载"""
    body = await request.json()

    # 兼容前端旧格式 { routes: { tiers: "..." } }
    if isinstance(body.get("routes"), dict):
        body = {**body, **body["routes"]}
    tiers = body.get("tiers")
    if isinstance(tiers, str):
        import json as _json
        try:
            tiers = _json.loads(tiers)
        except _json.JSONDecodeError:
            tiers = None
    if isinstance(tiers, dict):
        for tier_name, tier_cfg in tiers.items():
            if not isinstance(tier_cfg, dict):
                continue
            body[f"routing_{tier_name}"] = tier_cfg.get("primary", "")
            body[f"routing_{tier_name}_fallback"] = tier_cfg.get("fallback", "")

    # 短名 → 环境变量名映射
    mapping = {
        'brain_primary': 'SWARM_MODEL_BRAIN_PRIMARY',
        'brain_fallback': 'SWARM_MODEL_BRAIN_FALLBACK',
        'routing_trivial': 'SWARM_MODEL_ROUTING_TRIVIAL',
        'routing_trivial_fallback': 'SWARM_MODEL_ROUTING_TRIVIAL_FALLBACK',
        'routing_medium': 'SWARM_MODEL_ROUTING_MEDIUM',
        'routing_medium_fallback': 'SWARM_MODEL_ROUTING_MEDIUM_FALLBACK',
        'routing_complex': 'SWARM_MODEL_ROUTING_COMPLEX',
        'routing_complex_fallback': 'SWARM_MODEL_ROUTING_COMPLEX_FALLBACK',
        'routing_multimodal': 'SWARM_MODEL_ROUTING_MULTIMODAL',
        'routing_multimodal_fallback': 'SWARM_MODEL_ROUTING_MULTIMODAL_FALLBACK',
    }

    # 构建 update_map（同 PUT /api/config 逻辑）
    update_map: dict[str, str] = {}
    for key, env_key in mapping.items():
        if key in body:
            val = str(body[key]).strip()
            if val:
                update_map[env_key] = val

    if not update_map:
        from swarm.models.router import ModelRouter
        router = ModelRouter()
        return {"status": "no_changes", "message": "未检测到有效变更", **router.get_routing_table()}

    # 更新 os.environ
    for k, v in update_map.items():
        os.environ[k] = v

    # 写入 .env 文件
    env_path = _app._PROJECT_ROOT / ".env"
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip().upper()
            if k in update_map:
                new_lines.append(f"{k}={update_map[k]}")
                updated_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for k, v in update_map.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")

    content = "\n".join(new_lines) + "\n"
    env_path.write_text(content, encoding="utf-8")

    _app.logger.info(f"Updated routing .env + os.environ with keys: {list(update_map.keys())}")

    # 重新加载配置
    _app.reload_config()
    _app.logger.info("Config reloaded after routing update")

    from swarm.models.router import ModelRouter
    router = ModelRouter()
    return {
        "status": "ok",
        "updated_keys": list(update_map.keys()),
        **router.get_routing_table(),
    }
