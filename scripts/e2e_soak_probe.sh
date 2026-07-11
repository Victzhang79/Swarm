#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# e2e_soak_probe.sh —— E2E 开跑前【六模型探活】常驻脚本（治本：别每轮手工造 soak_probe.py）
#
# ★用 swarm 自己的 ModelRouter 探活（权威）★：真实 key 在 secret_store（.env 里为空占位），
# standalone curl 拿不到 key 必 401 假红。ModelRouter() 内部走 config→secret_store 解密，与真实
# 跑一致 → 能抓到"SWARM_SECRET_KEY 轮换/密文损坏 → 回退空 key → 401"这类真 blocker（round17→18
# 之间实测发生过，soak 拦住避免白跑一轮）。
#
# 探"持续可用"用 --sustained N：间隔 20s 连探 N 次，防 T0 单点假绿（round14 教训）。
# 用法:  scripts/e2e_soak_probe.sh [--sustained N]
# 退出:  0=全绿  1=有模型不可达（打印红名单 + 常见根因，禁止开跑）
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
PKG_DIR="/Users/zhangyanrui/LLM/swarm/swarm"
cd "$PKG_DIR" || { echo "[soak] ❌ 找不到包目录 $PKG_DIR"; exit 1; }
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
ROUNDS=1
[ "${1:-}" = "--sustained" ] && ROUNDS="${2:-3}"

"$PY" - "$ROUNDS" <<'PYEOF'
import sys, importlib.util, time
from pathlib import Path
_bs = Path("test/swarm_bootstrap.py")
_s = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_m = importlib.util.module_from_spec(_s); _s.loader.exec_module(_m)
from swarm.models.router import ModelRouter

# ★先把 .env 全量载进 os.environ（含 SWARM_SECRET_KEY）★：swarm_bootstrap 不加载 SWARM_SECRET_KEY，
# 缺它 → secret_store 解密失败 → 回退 .env 空 key → 假 401 假红（实测踩过）。与真实 api(restart-api
# source .env)一致后，探活才反映真实可用性。
import os as _os
for _line in open(".env"):
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    _os.environ.setdefault(_k, _v.strip().strip('"').strip("'"))

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 1
# ★模型清单从 config 动态派生（round41 治本）★：写死清单会与 .env 路由漂移——
# 本脚本要抓的"③路由名与网关不一致"曾发生在脚本自身（Qwopus 下线后仍探旧名假红）。
from swarm.config.settings import get_config
_mc = get_config().model
MODELS = []
for _name in [
    _mc.brain_primary, _mc.brain_fallback, _mc.worker_primary, _mc.worker_fallback,
    _mc.routing_trivial, _mc.routing_medium, _mc.routing_complex, _mc.routing_multimodal,
    # fallback 链也探（复核 LOW 补）：切备时真会上场（round41 Kimi 救回实证），
    # 漏探=切备时才发现备用不可达。worker_local(Ollama 遗留字段)不探：不在网关路由面。
    *_mc.routing_trivial_fallback, *_mc.routing_medium_fallback,
    *_mc.routing_complex_fallback, *_mc.routing_multimodal_fallback,
    *(_mc.model_providers or {}).keys(),
]:
    _name = (_name or "").strip()
    if _name and _name not in MODELS:
        MODELS.append(_name)
router = ModelRouter()
overall_ok = True
for r in range(1, rounds + 1):
    print(f"── soak 探活 第 {r}/{rounds} 轮 ──")
    for m in MODELS:
        try:
            resp = router.get_model_by_name(m, temperature=0).invoke("hi")
            ok = getattr(resp, "content", None) is not None
            print(f"  {'✅' if ok else '⚠️ 空响应'} {m}")
            overall_ok = overall_ok and ok
        except Exception as e:
            print(f"  ❌ {m} :: {str(e)[:100]}")
            overall_ok = False
    if r < rounds:
        time.sleep(20)

if overall_ok:
    print("[soak] ✅ 六模型全绿，可开跑")
    sys.exit(0)
print("[soak] ❌ 有模型不可达，禁止开跑（否则污染本轮判读）")
print("[soak] 常见根因：①SWARM_SECRET_KEY 轮换/密文损坏→secret_store 解密失败回退 .env 空 key→401")
print("[soak]           ②模型机/网关宕或卸载模型  ③.env 路由模型名与网关不一致")
sys.exit(1)
PYEOF
