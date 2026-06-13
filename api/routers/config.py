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
from swarm.api._shared import (
    _flatten_model_config,
    _mask_config_dict,
    _require_perm,
    _require_user,
    _resolve_key,
)

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
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
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
async def get_notify_channels():
    """当前通知渠道列表 + 预置类型目录 + 可订阅事件目录。api_key/webhook_url 脱敏。"""
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
    env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
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
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        # 同步 os.environ
        for k in cleared:
            if k in ("SWARM_MODEL_SILICONFLOW_API_KEY", "SWARM_MODEL_LOCAL_API_KEY"):
                os.environ[k] = ""
    return cleared
