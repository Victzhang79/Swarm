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
from swarm.config.settings import atomic_write_env
from swarm.api._shared import (
    _flatten_model_config,
    _mask_config_dict,
    _require_perm,
    _require_user,
    _resolve_key,
)

router = APIRouter()


@router.get("/api/config", tags=["配置"])
async def get_config_endpoint(request: Request):
    """返回当前配置（脱敏 API Key）"""
    _require_user(request)  # A-P0-7：配置读取需鉴权（泄露 provider/路由拓扑）
    cfg = _app.get_config()
    raw = cfg.model_dump()
    masked = _mask_config_dict(raw)
    flat = _mask_config_dict(_flatten_model_config(cfg))
    from swarm.tracing import langsmith_status

    return {"config": masked, "flat": flat, "langsmith": langsmith_status()}


# ─── 3.5 GET /api/models ─────────────────────────
@router.get("/api/models", tags=["配置"])
async def list_models(request: Request):
    """拉取所有已配 providers 的可用模型列表。

    遍历 _effective_providers（真相源），每个 provider 调其 base_url 的 OpenAI 兼容
    /models 端点（本地额外尝试 Open WebUI /api/models、Ollama /api/tags）。
    返回：
      - by_provider: {<provider_id>: {"label","kind","models":[...],"error"?}}  ← 新结构，支持任意多接入点
      - siliconflow / local: [...]  ← 向后兼容旧前端（保留）
      - siliconflow_error / local_error  ← 向后兼容
    单个 provider 不可达不影响其它（各自 try）。
    """
    _require_user(request)  # A-P0-7：模型清单需鉴权（泄露 provider 拓扑/端点）
    import asyncio

    import httpx

    cfg = _app.get_config()
    providers = list(cfg.model._effective_providers() or [])

    async def _fetch_one(pid: str, label: str, kind: str, base_url: str, api_key: str) -> dict:
        """拉单个 provider 的模型。返回 {label,kind,models,error?}。"""
        entry = {"label": label or pid, "kind": kind or "cloud", "models": []}
        if not base_url:
            entry["error"] = "未配置 base_url"
            return entry
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base = base_url.rstrip("/")
        # 候选端点：标准 OpenAI /models（base 已含 /v1 时直接用）；本地兼容 Open WebUI / Ollama
        root = base.removesuffix("/v1").removesuffix("/api")
        candidates = [f"{base}/models", f"{root}/v1/models", f"{root}/api/models", f"{root}/api/tags"]
        seen = set()
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as client:
                for ep in candidates:
                    if ep in seen:
                        continue
                    seen.add(ep)
                    try:
                        resp = await client.get(ep, headers=headers)
                    except Exception:  # noqa: BLE001 — 端点不通试下一个
                        continue
                    if resp.status_code == 200:
                        data = resp.json()
                        raw = data.get("data", data.get("models", []))
                        models = sorted(
                            m.get("id", m.get("name", "")) for m in raw if m.get("id") or m.get("name")
                        )
                        if models:
                            entry["models"] = models
                            return entry
                    elif resp.status_code in (401, 403):
                        entry["error"] = "认证失败：请检查 API Key"
                        return entry
                if not entry["models"] and "error" not in entry:
                    entry["error"] = "未返回模型列表"
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)
        return entry

    # 并发拉所有 provider
    tasks = [
        _fetch_one(
            getattr(p, "id", ""),
            getattr(p, "label", "") or getattr(p, "id", ""),
            getattr(p, "kind", "cloud"),
            getattr(p, "base_url", "") or "",
            getattr(p, "api_key", "") or "",
        )
        for p in providers
        if getattr(p, "id", "")
    ]
    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    by_provider: dict[str, dict] = {}
    for p, res in zip([pp for pp in providers if getattr(pp, "id", "")], fetched):
        pid = getattr(p, "id", "")
        if isinstance(res, Exception):
            by_provider[pid] = {"label": getattr(p, "label", "") or pid,
                                "kind": getattr(p, "kind", "cloud"),
                                "models": [], "error": str(res)}
        else:
            by_provider[pid] = res

    # 向后兼容：保留 siliconflow / local 扁平字段（旧前端仍读）
    result: dict[str, Any] = {"by_provider": by_provider}
    for compat_id in ("siliconflow", "local"):
        ent = by_provider.get(compat_id)
        if ent is not None:
            result[compat_id] = ent.get("models", [])
            if ent.get("error"):
                result[f"{compat_id}_error"] = ent["error"]
        else:
            result[compat_id] = []
    return result


@router.post("/api/config/test", tags=["配置"])
async def test_config(request: Request):
    """测试 Brain / 本地 Worker / 云端 Worker 模型是否可调用"""
    _require_perm(request, "config:write")  # A-P0-7：触发出站模型探活=需写权限
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

    S4 修复：补鉴权——此端点把任意 KEY=VALUE 写进 .env，可被滥用篡改 base_url 钓 key。

    接收格式:
    - {"config": {"siliconflow_api_key": "...", ...}}
    - {"siliconflow_api_key": "...", ...}
    """
    _require_perm(request, "config:write")
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

    # 写回 .env（A-P1-29：原子写）
    content = "\n".join(new_lines) + "\n"
    atomic_write_env(env_path, content)

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
async def get_routing(request: Request):
    """获取当前模型路由表"""
    _require_user(request)  # A-P0-7：路由表需鉴权（泄露内部模型拓扑）
    from swarm.models.router import ModelRouter
    router = ModelRouter()
    return router.get_routing_table()


# ─── 4.6 PUT /api/routing ────────────────────────────
@router.put("/api/routing", tags=["配置"])
async def update_routing(request: Request):
    """更新模型路由表配置 — 写入 .env 并重载"""
    _require_perm(request, "config:write")  # S4 修复：补鉴权
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
            raw = body[key]
            # fallback 链可能是 list（前端多级兜底链）→ 存成逗号链，供 _coerce_model_list 还原。
            # 不能用 str(list)（会得到 "['A', 'B']" 单引号非法 JSON）。
            if isinstance(raw, (list, tuple)):
                val = ",".join(str(x).strip() for x in raw if str(x).strip())
            else:
                val = str(raw).strip()
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
    atomic_write_env(env_path, content)

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


# ─── 4.7 PUT /api/model-providers ────────────────────────────
@router.put("/api/model-providers", tags=["配置"])
async def update_model_providers(request: Request):
    """更新模型接入点(providers) + 模型归属(model_providers) + 规模标签(model_sizes)。

    这些是复杂结构(list/dict)，以 JSON 写入 .env 的 SWARM_MODEL_PROVIDERS /
    SWARM_MODEL_MODEL_PROVIDERS / SWARM_MODEL_MODEL_SIZES —— pydantic-settings
    从 env 读 list/dict 字段时按 JSON 解析。

    Body: {
      "providers": [{"id","label","kind","base_url","api_key","max_retries"}, ...],
      "model_providers": {"<model>": "<provider_id>", ...},
      "model_sizes": {"<model>": "large|small", ...}
    }
    api_key 为脱敏值(***)的 provider 会保留原 key（不覆盖）。
    """
    _require_perm(request, "config:write")  # S4 修复：补鉴权
    import json as _json

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    cur = _app.get_config().model
    update_map: dict[str, str] = {}

    # providers：合并脱敏 key（前端回传 *** 时保留原 key）
    if isinstance(body.get("providers"), list):
        old_by_id = {p.id: p for p in cur._effective_providers()}
        clean: list[dict] = []
        for p in body["providers"]:
            if not isinstance(p, dict) or not p.get("id"):
                continue
            key = str(p.get("api_key", "") or "")
            if "***" in key or (key == "" and p["id"] in old_by_id):
                # 脱敏或空 → 保留原 key
                _old = old_by_id.get(p["id"])
                key = _old.api_key if _old is not None else ""
            entry = {
                "id": str(p["id"]).strip(),
                "label": str(p.get("label", "") or ""),
                "kind": p.get("kind", "cloud") if p.get("kind") in ("cloud", "local") else "cloud",
                "base_url": str(p.get("base_url", "") or ""),
                "api_key": key,
            }
            if p.get("max_retries") is not None:
                try:
                    entry["max_retries"] = int(p["max_retries"])
                except (ValueError, TypeError):
                    pass
            clean.append(entry)

        # 敏感信息加密存 db：把每个 provider 的 api_key 加密入 secret_store，
        # 写进 .env 的 JSON 里 key 字段【清空】（不再明文落盘）。读取时 _effective_providers
        # 从 db 解密补回。db 写失败则回退原行为（明文进 .env），保证不丢配置。
        try:
            from swarm.config import secret_store

            for entry in clean:
                key_val = entry.get("api_key", "")
                if key_val:
                    secret_store.set_secret(f"provider_api_key:{entry['id']}", key_val)
                    entry["api_key"] = ""  # .env 里不留明文
            _app.logger.info("provider api_keys 已加密存入 secret_store")
        except Exception as exc:  # noqa: BLE001
            _app.logger.warning("secret_store 写入失败，回退明文 .env: %s", exc)

        update_map["SWARM_MODEL_PROVIDERS"] = _json.dumps(clean, ensure_ascii=False)

        # B 方案：providers 是唯一真相源，但 /api/models 等老读取点仍读扁平字段。
        # 把内置 id(siliconflow/local) 的 base_url 同步回写老字段（key 已转 db，不写明文）。
        for entry in clean:
            if entry["id"] == "siliconflow":
                update_map["SWARM_MODEL_SILICONFLOW_BASE_URL"] = entry["base_url"]
            elif entry["id"] == "local":
                update_map["SWARM_MODEL_LOCAL_BASE_URL"] = entry["base_url"]

    if isinstance(body.get("model_providers"), dict):
        mp = {str(k): str(v) for k, v in body["model_providers"].items() if v}
        update_map["SWARM_MODEL_MODEL_PROVIDERS"] = _json.dumps(mp, ensure_ascii=False)

    if isinstance(body.get("model_sizes"), dict):
        ms = {str(k): str(v) for k, v in body["model_sizes"].items() if v in ("large", "small")}
        update_map["SWARM_MODEL_MODEL_SIZES"] = _json.dumps(ms, ensure_ascii=False)

    if not update_map:
        raise HTTPException(status_code=400, detail="无 providers/model_providers/model_sizes 字段")

    # 写 .env + 同步 os.environ
    env_path = _app._PROJECT_ROOT / ".env"
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip().upper()
            if k in update_map:
                new_lines.append(f"{k}={update_map[k]}")
                updated_keys.add(k)
                continue
        new_lines.append(line)
    for k, v in update_map.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")
    atomic_write_env(env_path, "\n".join(new_lines) + "\n")
    for k, v in update_map.items():
        os.environ[k] = v

    from swarm.config.settings import reload_config as _reload_config
    _reload_config()
    try:
        from swarm.config import secret_store
        secret_store.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass
    _app.logger.info("Updated model providers: %s", list(update_map.keys()))

    from swarm.models.router import ModelRouter
    return {"status": "ok", "updated_keys": list(update_map.keys()), **ModelRouter().get_routing_table()}


def _persist_env_updates(update_map: dict[str, str]) -> None:
    """写 .env + 同步 os.environ + reload_config（抽取自 update_model_providers，供 kb 端点等复用）。"""
    env_path = _app._PROJECT_ROOT / ".env"
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated_keys: set[str] = set()
    new_lines: list[str] = []
    for line in existing_lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip().upper()
            if k in update_map:
                new_lines.append(f"{k}={update_map[k]}")
                updated_keys.add(k)
                continue
        new_lines.append(line)
    for k, v in update_map.items():
        if k not in updated_keys:
            new_lines.append(f"{k}={v}")
    atomic_write_env(env_path, "\n".join(new_lines) + "\n")
    for k, v in update_map.items():
        os.environ[k] = v
    from swarm.config.settings import reload_config as _reload_config
    _reload_config()


# ─── 4.9 Embed/Rerank 接入点配置（方案 A，docs/Embed_Rerank_Config_Design.md）────
@router.get("/api/kb/embed-rerank/catalog", tags=["配置"])
async def kb_embed_rerank_catalog():
    """embed/rerank 预置接入点目录 —— 前端下拉用，选中自动填 base_url/model/format。"""
    from swarm.knowledge.embed_rerank_config import EMBED_CATALOG, RERANK_CATALOG
    return {"embed": EMBED_CATALOG, "rerank": RERANK_CATALOG}


@router.get("/api/kb/embed-rerank", tags=["配置"])
async def get_kb_embed_rerank(request: Request):
    """读 embed/rerank 当前配置（key 脱敏，仅返回是否已配 has_key）。"""
    _require_user(request)  # A-P0-7：embed/rerank 端点配置需鉴权
    from swarm.config.settings import KnowledgeConfig
    from swarm.knowledge.embed_rerank_config import get_embed_endpoint, get_rerank_endpoint
    k = KnowledgeConfig()
    # has_key：解析后的有效 key（含 secret_store/复用）是否非空
    e_ep = get_embed_endpoint()
    r_ep = get_rerank_endpoint()
    return {
        "embed": {
            "base_url": k.embed_base_url, "model": k.embedding_model, "format": k.embed_format,
            "reuse_provider": k.embed_reuse_provider, "has_key": bool(e_ep and e_ep.api_key),
            "batch_size": k.embed_batch_size,
        },
        "rerank": {
            "url": k.rerank_url, "model": k.reranker_model, "format": k.rerank_format,
            "reuse_provider": k.rerank_reuse_provider, "has_key": bool(r_ep and r_ep.api_key),
            "score_threshold": k.rerank_score_threshold,
        },
        # 检索调优参数（top_k / 阈值 / 切块）—— 全部可在 WebUI 配置，保存即 reload
        "retrieval": {
            "retrieval_top_k": k.retrieval_top_k,
            "rerank_top_k": k.rerank_top_k,
            "semantic_score_threshold": k.semantic_score_threshold,
            "priority_file_top_k": k.priority_file_top_k,
            "max_priority_files": k.max_priority_files,
            "chunk_size": k.chunk_size,
            "chunk_overlap": k.chunk_overlap,
        },
    }


@router.put("/api/kb/embed-rerank", tags=["配置"])
async def update_kb_embed_rerank(request: Request):
    """更新 embed/rerank 接入点。非敏感写 .env(SWARM_KB_*)，key 加密入 secret_store。

    Body: {
      "embed":  {"base_url","model","format","reuse_provider","api_key"?,"batch_size"?},
      "rerank": {"url","model","format","reuse_provider","api_key"?,"score_threshold"?}
    }
    api_key 为 *** 或省略 → 保留原 key（不覆盖）。
    返回 dim_changed 提示：embed 维度可能变化时（模型变了）提醒重新预处理。
    """
    _require_perm(request, "config:write")  # S4 修复：补鉴权
    from swarm.config import secret_store
    from swarm.config.settings import KnowledgeConfig
    from swarm.knowledge.embed_rerank_config import SECRET_EMBED_KEY, SECRET_RERANK_KEY

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    old = KnowledgeConfig()
    update_map: dict[str, str] = {}
    old_embed_model = old.embedding_model

    emb = body.get("embed") or {}
    if emb:
        if "base_url" in emb:
            update_map["SWARM_KB_EMBED_BASE_URL"] = str(emb.get("base_url") or "")
        if emb.get("model"):
            update_map["SWARM_KB_EMBEDDING_MODEL"] = str(emb["model"])
        if emb.get("format"):
            update_map["SWARM_KB_EMBED_FORMAT"] = str(emb["format"])
        update_map["SWARM_KB_EMBED_REUSE_PROVIDER"] = str(emb.get("reuse_provider") or "")
        if emb.get("batch_size") is not None:
            update_map["SWARM_KB_EMBED_BATCH_SIZE"] = str(int(emb["batch_size"]))
        ekey = str(emb.get("api_key", "") or "")
        if ekey and "***" not in ekey:
            secret_store.set_secret(SECRET_EMBED_KEY, ekey)
            update_map["SWARM_KB_EMBED_API_KEY"] = ""  # 不留明文

    rk = body.get("rerank") or {}
    if rk:
        if "url" in rk:
            update_map["SWARM_KB_RERANK_URL"] = str(rk.get("url") or "")
        if rk.get("model"):
            update_map["SWARM_KB_RERANKER_MODEL"] = str(rk["model"])
        if rk.get("format"):
            update_map["SWARM_KB_RERANK_FORMAT"] = str(rk["format"])
        update_map["SWARM_KB_RERANK_REUSE_PROVIDER"] = str(rk.get("reuse_provider") or "")
        if rk.get("score_threshold") is not None:
            update_map["SWARM_KB_RERANK_SCORE_THRESHOLD"] = str(float(rk["score_threshold"]))
        rkey = str(rk.get("api_key", "") or "")
        if rkey and "***" not in rkey:
            secret_store.set_secret(SECRET_RERANK_KEY, rkey)
            update_map["SWARM_KB_RERANK_API_KEY"] = ""

    # 检索调优参数（top_k / 阈值 / 切块）—— 整型/浮点，带范围保护防误填
    rt = body.get("retrieval") or {}
    if rt:
        def _pint(name: str, env: str, lo: int, hi: int) -> None:
            if rt.get(name) is not None:
                try:
                    val = int(rt[name])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{name} 必须为整数")
                if not (lo <= val <= hi):
                    raise HTTPException(status_code=400, detail=f"{name} 应在 [{lo}, {hi}]")
                update_map[env] = str(val)

        def _pfloat(name: str, env: str, lo: float, hi: float) -> None:
            if rt.get(name) is not None:
                try:
                    val = float(rt[name])
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{name} 必须为数字")
                if not (lo <= val <= hi):
                    raise HTTPException(status_code=400, detail=f"{name} 应在 [{lo}, {hi}]")
                update_map[env] = str(val)

        _pint("retrieval_top_k", "SWARM_KB_RETRIEVAL_TOP_K", 1, 500)
        _pint("rerank_top_k", "SWARM_KB_RERANK_TOP_K", 1, 100)
        _pfloat("semantic_score_threshold", "SWARM_KB_SEMANTIC_SCORE_THRESHOLD", 0.0, 1.0)
        _pint("priority_file_top_k", "SWARM_KB_PRIORITY_FILE_TOP_K", 0, 50)
        _pint("max_priority_files", "SWARM_KB_MAX_PRIORITY_FILES", 0, 50)
        _pint("chunk_size", "SWARM_KB_CHUNK_SIZE", 64, 4096)
        _pint("chunk_overlap", "SWARM_KB_CHUNK_OVERLAP", 0, 1024)

    if not update_map:
        raise HTTPException(status_code=400, detail="无 embed/rerank 字段")

    _persist_env_updates(update_map)
    try:
        secret_store.invalidate_cache()
    except Exception:  # noqa: BLE001
        pass

    # 维度变更提示：embedding 模型变了 → 已有 Qdrant 向量可能维度不符，需重新预处理（用户决定）。
    # 直接比较 body 传入的新模型 vs 旧模型（不依赖 reload 时序，更可靠）。
    new_embed_model = str(emb.get("model") or "") if emb else ""
    dim_changed = bool(new_embed_model and new_embed_model != old_embed_model)
    _app.logger.info("Updated kb embed/rerank: %s (dim_changed=%s)", list(update_map.keys()), dim_changed)
    return {"status": "ok", "updated_keys": list(update_map.keys()), "embed_model_changed": dim_changed,
            "reprocess_hint": "embedding 模型已变更，建议重新预处理所有项目以重建向量" if dim_changed else ""}


# ─── 4.8 GET /api/model-providers/catalog ────────────────────
@router.get("/api/model-providers/catalog", tags=["配置"])
async def model_providers_catalog():
    """预置云端接入点目录 —— 前端"添加接入点"下拉用，选中自动填 base_url/label/kind。

    base_url 来自 Hermes-Agent 源码的权威端点（OpenAI 兼容）。
    """
    from swarm.config.settings import KNOWN_PROVIDERS
    return {"catalog": KNOWN_PROVIDERS}


# ─── 5. 通知渠道 ─────────────────────────────────────
@router.get("/api/notify-channels", tags=["配置"])
async def get_notify_channels(request: Request):
    """当前通知渠道列表 + 预置类型目录 + 可订阅事件目录。api_key/webhook_url 脱敏。"""
    _require_user(request)  # A-P0-7：通知渠道配置需鉴权（含脱敏 webhook）
    from swarm.config.settings import KNOWN_NOTIFY_TYPES, NOTIFY_EVENT_TYPES
    cfg = _app.get_config()
    channels = []
    for ch in cfg.notify_channels:
        url = ch.webhook_url or ""
        # 脱敏 webhook_url（含 token）：保留协议+host 头尾
        masked = url
        if len(url) > 24:
            masked = url[:20] + "…" + url[-6:]
        channels.append({
            "id": ch.id, "type": ch.type, "label": ch.label,
            "webhook_url_masked": masked, "has_url": bool(url),
            "enabled": ch.enabled, "events": list(ch.events), "user_id": ch.user_id,
        })
    return {"channels": channels, "catalog": KNOWN_NOTIFY_TYPES, "event_types": NOTIFY_EVENT_TYPES}


@router.put("/api/notify-channels", tags=["配置"])
async def update_notify_channels(request: Request):
    """更新通知渠道列表 → 写 SWARM_NOTIFY_CHANNELS(JSON) 到 .env + reload。

    Body: {"channels": [{id,type,label,webhook_url,enabled,events,user_id}, ...]}
    webhook_url 为脱敏值(含 …)或空 → 保留原 url（不覆盖）。
    """
    _require_perm(request, "config:write")  # S4 修复：补鉴权
    import json as _json
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("channels"), list):
        raise HTTPException(status_code=400, detail="需要 channels 列表")

    cur = _app.get_config()
    old_by_id = {c.id: c for c in cur.notify_channels}
    valid_types = {"feishu", "dingtalk", "wecom", "slack", "generic"}

    clean: list[dict] = []
    for c in body["channels"]:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        cid = str(c["id"]).strip()
        url = str(c.get("webhook_url", "") or "")
        # 脱敏或空 → 保留原 url
        if "…" in url or "***" in url or (url == "" and cid in old_by_id):
            _old = old_by_id.get(cid)
            url = _old.webhook_url if _old is not None else ""
        ctype = c.get("type", "generic")
        if ctype not in valid_types:
            ctype = "generic"
        events = c.get("events") or []
        events = [str(e) for e in events if isinstance(e, str)] if isinstance(events, list) else []
        clean.append({
            "id": cid,
            "type": ctype,
            "label": str(c.get("label", "") or ""),
            "webhook_url": url,
            "enabled": bool(c.get("enabled", True)),
            "user_id": str(c.get("user_id", "") or ""),
            "events": events,
        })

    env_path = _app._PROJECT_ROOT / ".env"
    env_key = "SWARM_NOTIFY_CHANNELS"
    val = _json.dumps(clean, ensure_ascii=False)
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    found = False
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s and s.partition("=")[0].strip().upper() == env_key:
            out.append(f"{env_key}={val}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{env_key}={val}")
    atomic_write_env(env_path, "\n".join(out) + "\n")
    os.environ[env_key] = val

    from swarm.config.settings import reload_config as _reload_config
    _reload_config()
    _app.logger.info("Updated notify channels: %d 个", len(clean))
    return {"status": "ok", "count": len(clean)}


@router.post("/api/notify-channels/test", tags=["配置"])
async def test_notify_channel(request: Request):
    """发送一条测试通知到指定渠道（或临时渠道配置），验证 webhook 可达。

    Body: {"channel_id": "ch1"}  或  {"type":"feishu","webhook_url":"..."}
    """
    _require_perm(request, "config:write")  # P0-SEC-04：补鉴权（原无授权即可触发出站请求=SSRF）
    from swarm.api.notify import _build_payload, _post_webhook
    body = await request.json()

    url = ""
    ctype = "generic"
    if body.get("channel_id"):
        cur = _app.get_config()
        ch = next((c for c in cur.notify_channels if c.id == body["channel_id"]), None)
        if ch is None:
            raise HTTPException(status_code=404, detail="渠道不存在")
        url, ctype = ch.webhook_url, ch.type
    else:
        url = str(body.get("webhook_url", "") or "").strip()
        ctype = body.get("type", "generic")
        # 临时测试时若 url 是脱敏值，回退到同 type 已存渠道的真 url
        if "…" in url or not url:
            raise HTTPException(status_code=400, detail="请填写有效 webhook_url")

    payload = _build_payload(ctype, "test", "", "这是一条来自 Swarm 的测试通知 ✅")
    ok = await _post_webhook(url, payload, tag="test")
    return {"status": "ok" if ok else "failed", "delivered": ok}


# ═══════════════════════════════════════════════════════════
# 模型能力探测与注册（设计 v3 A 部分 / A批2）
# ═══════════════════════════════════════════════════════════

# 探测 job 内存 registry（探测是瞬时操作，进程内存足够；能力结果本身落
# model_capabilities 表已持久化）。key=provider_id。前端轮询 status 端点。
_PROBE_JOBS: dict[str, dict[str, Any]] = {}


def _provider_by_id(provider_id: str):
    """从生效 provider 列表里按 id 找一个 ProviderConfig；找不到返回 None。"""
    cfg = _app.get_config()
    for p in cfg.model._effective_providers():
        if p.id == provider_id:
            return p
    return None


@router.post("/api/models/probe", tags=["配置"])
async def probe_models(request: Request):
    """触发对某 provider 模型的能力探测（异步，立即返回 job）。

    设计 A.4：探测有副作用（真实 API 调用花 token/算力），必须用户显式触发；
    不阻塞 —— 立即返回 job，后台异步探测写库，前端轮询 status。

    scope：
      - 省略（默认）= "auto"：按接入点类型智能决定 ——
          * local（本地推理，免费）→ 全探（发现全部可用模型）
          * cloud（云端 API，按 token 计费）→ 只探路由策略在用的模型（省钱）
      - "in_use"：强制只探在用模型（任何接入点）。
      - "all"：强制全探（任何接入点；云端慎用，几十上百模型费 token）。
    Body: {"provider_id": "siliconflow", "scope": "auto"}
    """
    _require_perm(request, "config:write")
    body = await request.json()
    provider_id = str(body.get("provider_id", "")).strip()
    if not provider_id:
        raise HTTPException(status_code=400, detail="缺少 provider_id")

    provider = _provider_by_id(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"接入点不存在: {provider_id}")

    scope = str(body.get("scope", "auto")).strip() or "auto"
    cfg = _app.get_config()

    # auto：本地全探、云端只探在用（按 token 成本差异决定）
    if scope == "auto":
        scope = "all" if provider.kind == "local" else "in_use"

    only_models: list[str] | None
    if scope == "all":
        only_models = None
    else:
        only_models = cfg.model.models_in_use_for_provider(provider_id)
        if not only_models:
            return {"status": "no_models_in_use", "provider_id": provider_id,
                    "message": "该接入点下没有在用模型（路由策略未引用），无需探测"}

    # 已在探测中则拒绝重复触发
    existing = _PROBE_JOBS.get(provider_id)
    if existing and existing.get("status") == "running":
        return {"status": "already_running", "job": existing}

    job = {
        "provider_id": provider_id,
        "status": "running",
        "scope": scope,
        "done": 0,
        "total": len(only_models) if only_models is not None else 0,
        "current": "",
        "error": None,
        "result": None,
    }
    _PROBE_JOBS[provider_id] = job

    measure_speed = bool(body.get("measure_speed", True))

    async def _run_probe():
        from swarm.models import prober

        def _cb(done, total, current):
            job["done"] = done
            job["total"] = total
            job["current"] = current

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: prober.probe_provider(
                    provider, only_models=only_models,
                    measure_speed=measure_speed,
                    persist=True, progress_cb=_cb,
                ),
            )
            job["result"] = {
                "total": result["total"],
                "probed": result["probed"],
                "errors": result.get("errors", []),
                "provider_error": result.get("error"),
            }
            # provider 级错误（如认证失败/端点不可达）= 探测失败，不能伪装成 done。
            if result.get("error"):
                job["status"] = "error"
                job["error"] = result["error"]
                _app.logger.warning(
                    "模型能力探测失败 provider=%s: %s", provider_id, result["error"],
                )
            else:
                job["status"] = "done"
                _app.logger.info(
                    "模型能力探测完成 provider=%s probed=%d/%d",
                    provider_id, result["probed"], result["total"],
                )
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)
            _app.logger.exception("模型能力探测失败 provider=%s", provider_id)

    asyncio.create_task(_run_probe())
    return {"status": "started", "job": job}


@router.get("/api/models/probe/status", tags=["配置"])
async def probe_status(provider_id: str, request: Request):
    """查询某 provider 的探测 job 状态（前端轮询用）。"""
    _require_user(request)
    job = _PROBE_JOBS.get(provider_id)
    if job is None:
        return {"status": "idle", "provider_id": provider_id}
    return job


@router.get("/api/models/capabilities", tags=["配置"])
async def get_capabilities(request: Request, provider_id: str | None = None):
    """读模型能力库（全部或按 provider 过滤）。"""
    _require_user(request)
    from swarm.models import capability_store as cap

    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(
        None, lambda: cap.list_capabilities(provider_id)
    )
    return {"capabilities": rows, "count": len(rows)}


@router.put("/api/models/capabilities", tags=["配置"])
async def update_capability(request: Request):
    """人工修正一条能力记录（A.4：探测失败/不准时手填，source=manual）。

    Body: {"provider_id":"x","model_id":"y","context_window":128000,
           "supports_multimodal":true,"gen_speed_tps":0,"kind":"local"}
    """
    _require_perm(request, "config:write")
    from swarm.models import capability_store as cap

    body = await request.json()
    provider_id = str(body.get("provider_id", "")).strip()
    model_id = str(body.get("model_id", "")).strip()
    if not provider_id or not model_id:
        raise HTTPException(status_code=400, detail="缺少 provider_id 或 model_id")

    loop = asyncio.get_running_loop()
    row = await loop.run_in_executor(
        None,
        lambda: cap.upsert_capability(
            provider_id, model_id,
            context_window=body.get("context_window"),
            supports_multimodal=bool(body.get("supports_multimodal", False)),
            gen_speed_tps=float(body.get("gen_speed_tps", 0.0) or 0.0),
            kind=str(body.get("kind", "cloud")),
            source=cap.SOURCE_MANUAL,
            note=str(body.get("note", "人工修正")),
        ),
    )
    return {"status": "ok", "capability": row}


@router.delete("/api/models/capabilities", tags=["配置"])
async def delete_capability_endpoint(request: Request, provider_id: str, model_id: str):
    """删一条能力记录（清理脏数据/重探前用）。"""
    _require_perm(request, "config:write")
    from swarm.models import capability_store as cap

    loop = asyncio.get_running_loop()
    deleted = await loop.run_in_executor(
        None, lambda: cap.delete_capability(provider_id, model_id)
    )
    return {"status": "ok", "deleted": deleted}


# ═══════════════════════════════════════════════════════════
# 敏感信息加密存储（API keys 加密存 db，明文不落 .env）
# ═══════════════════════════════════════════════════════════

@router.get("/api/secrets/status", tags=["配置"])
async def secrets_status(request: Request):
    """敏感信息存储状态：哪些 key 已加密入 db、根密钥来源。"""
    _require_user(request)
    from swarm.config import secret_store

    loop = asyncio.get_running_loop()
    names = await loop.run_in_executor(None, secret_store.list_secret_names)
    has_env_key = bool(os.environ.get("SWARM_SECRET_KEY", "").strip())
    return {
        "stored_secrets": names,
        "count": len(names),
        "root_key_source": "env(SWARM_SECRET_KEY)" if has_env_key else "db派生(弱,建议设SWARM_SECRET_KEY)",
    }


@router.post("/api/secrets/migrate", tags=["配置"])
async def migrate_secrets_to_db(request: Request):
    """一键迁移：把 .env 里现存的明文 API keys 加密入 db，并从 .env 清除明文。

    扫描 providers 的 api_key + 扁平字段（siliconflow/local），加密存 secret_store，
    然后把 .env 里对应明文清空。已在 db 的不重复迁移。
    """
    _require_perm(request, "config:write")
    from swarm.config import secret_store

    cfg = _app.get_config().model
    loop = asyncio.get_running_loop()
    migrated: list[str] = []

    def _do_migrate():
        # 1) 显式 providers 的 key
        for p in (cfg.providers or []):
            # 直接读原始配置的明文（绕过 _effective_providers 的 db 解密）
            if p.api_key and "***" not in p.api_key:
                secret_store.set_secret(f"provider_api_key:{p.id}", p.api_key)
                migrated.append(f"provider_api_key:{p.id}")
        # 2) 扁平字段（合成 provider）
        if cfg.siliconflow_api_key:
            secret_store.set_secret("provider_api_key:siliconflow", cfg.siliconflow_api_key)
            migrated.append("provider_api_key:siliconflow")
        if cfg.local_api_key:
            secret_store.set_secret("provider_api_key:local", cfg.local_api_key)
            migrated.append("provider_api_key:local")

    await loop.run_in_executor(None, _do_migrate)

    # 从 .env 清除已迁移的明文 key（SWARM_MODEL_PROVIDERS 的 JSON + 扁平字段）
    cleared = await loop.run_in_executor(None, _clear_plaintext_keys_from_env)

    # reload + 失效缓存
    from swarm.config.settings import reload_config as _reload_config
    await loop.run_in_executor(None, _reload_config)
    await loop.run_in_executor(None, secret_store.invalidate_cache)

    return {
        "status": "ok",
        "migrated": list(dict.fromkeys(migrated)),
        "env_cleared": cleared,
        "message": "明文 key 已加密入 db 并从 .env 清除",
    }


def _clear_plaintext_keys_from_env() -> list[str]:
    """把 .env 里的明文 API key 字段清空（迁移到 db 后）。返回被清的字段名。"""
    import json as _json

    env_path = _app._PROJECT_ROOT / ".env"
    if not env_path.exists():
        return []
    cleared: list[str] = []
    lines = env_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            k = k.strip()
            ku = k.upper()
            # 扁平 key 字段 → 清空
            if ku in ("SWARM_MODEL_SILICONFLOW_API_KEY", "SWARM_MODEL_LOCAL_API_KEY") and v.strip():
                out.append(f"{k}=")
                cleared.append(k)
                continue
            # SWARM_MODEL_PROVIDERS 的 JSON → 清掉每个 provider 的 api_key
            if ku == "SWARM_MODEL_PROVIDERS" and v.strip():
                try:
                    arr = _json.loads(v)
                    changed = False
                    for entry in arr:
                        if isinstance(entry, dict) and entry.get("api_key"):
                            entry["api_key"] = ""
                            changed = True
                    if changed:
                        out.append(f"{k}={_json.dumps(arr, ensure_ascii=False)}")
                        cleared.append(k)
                        continue
                except Exception:  # noqa: BLE001
                    pass
        out.append(line)
    if cleared:
        atomic_write_env(env_path, "\n".join(out) + "\n")
        # 同步 os.environ
        for k in cleared:
            if k in ("SWARM_MODEL_SILICONFLOW_API_KEY", "SWARM_MODEL_LOCAL_API_KEY"):
                os.environ[k] = ""
    return cleared
