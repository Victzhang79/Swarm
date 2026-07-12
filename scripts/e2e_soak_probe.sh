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
_wc = get_config().worker
MODELS = []
for _name in [
    _mc.brain_primary, _mc.brain_fallback, _mc.worker_primary, _mc.worker_fallback,
    # 复核 F5：并行池主力必须显式探（今天恰与 trivial 链重合是巧合，指向链外模型即漂移盲区）
    *(getattr(_wc, "worker_parallel_pool", None) or []),
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
# 模型健康表：任一轮失败即判该模型不健康（sustained 语义=全程稳定才算绿）
healthy = {m: True for m in MODELS}
for r in range(1, rounds + 1):
    print(f"── soak 探活 第 {r}/{rounds} 轮 ──")
    for m in MODELS:
        try:
            resp = router.get_model_by_name(m, temperature=0).invoke("hi")
            ok = getattr(resp, "content", None) is not None
            print(f"  {'✅' if ok else '⚠️ 空响应'} {m}")
            healthy[m] = healthy[m] and ok
        except Exception as e:
            print(f"  ❌ {m} :: {str(e)[:100]}")
            healthy[m] = False
    if r < rounds:
        time.sleep(20)

# ★链覆盖闸门（round45 用户拍板治本）★：自建模型临时掉线（修 10min 又恢复）是常态，
# 单模型红不再阻断起跑——运行期有熔断半开+fallback 链兜底，掉线模型恢复后自动回场。
# 只有【某条路由链全灭】（主备无一健康）才禁跑：那才是真跑必死面。
CHAINS = {
    "brain":      [_mc.brain_primary, _mc.brain_fallback],
    "worker":     [_mc.worker_primary, _mc.worker_fallback],
    "trivial":    [_mc.routing_trivial, *_mc.routing_trivial_fallback],
    "medium":     [_mc.routing_medium, *_mc.routing_medium_fallback],
    "complex":    [_mc.routing_complex, *_mc.routing_complex_fallback],
    "multimodal": [_mc.routing_multimodal, *_mc.routing_multimodal_fallback],
}
down = [m for m, ok in healthy.items() if not ok]
dead_chains = []
for role, chain in CHAINS.items():
    names = [n.strip() for n in chain if n and n.strip()]
    if names and not any(healthy.get(n, False) for n in names):
        dead_chains.append(role)

if not down:
    print(f"[soak] ✅ 全部 {len(MODELS)} 模型全绿，可开跑")
    sys.exit(0)
if not dead_chains:
    print(f"[soak] ⚠️ {len(down)} 个模型不可达但每条路由链仍有健康兜底 → 允许开跑（降级模式）")
    print(f"[soak]   掉线名单: {down}")
    print("[soak]   运行期由熔断半开+fallback 兜底；掉线模型恢复后自动回场（无需改配置）")
    sys.exit(0)
print(f"[soak] ❌ 路由链全灭: {dead_chains}（主备无一健康）→ 禁止开跑（真跑必死面）")
print(f"[soak]   掉线名单: {down}")
print("[soak] 常见根因：①SWARM_SECRET_KEY 轮换/密文损坏→secret_store 解密失败回退 .env 空 key→401")
print("[soak]           ②模型机/网关宕或卸载模型  ③.env 路由模型名与网关不一致")
sys.exit(1)
PYEOF
