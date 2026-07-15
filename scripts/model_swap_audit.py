#!/usr/bin/env python3
"""model_swap_audit.py — 模型下线换装审计器。

治本诉求（用户 2026-07-15）：模型下线/换装时，别再遍历代码和数据库、在提醒下一个个找落点。
一条命令列齐【所有模型名会出现的配置面】+ 当前 live 路由快照 + 残留检查 + 运维清单。

★必须用包内解释器跑（读得到 swarm 包 + .env）★：
    .venv/bin/python scripts/model_swap_audit.py [--retired A,B] [--new X]
裸 `python3 scripts/model_swap_audit.py` 载不到 swarm 包 → 本脚本【硬失败退非零】，绝不假绿。

用法:
  .venv/bin/python scripts/model_swap_audit.py                  落点清单 + live 路由快照 + 运维清单
  .venv/bin/python scripts/model_swap_audit.py --retired A,B    断言 A,B 已从【全仓】清除；残留 → 退出码 1
  .venv/bin/python scripts/model_swap_audit.py --retired A --new X  另提示新模型 X 的能力启发式下游

设计：落点分四类——①【权威配置】(必须手改) ②【派生/缓存】(自动跟随，只需刷新) ③【运行期/DB】
(需 restart / 重探 / 清行) ④【测试/文档/前端】(硬编码断言或示例)。残留检查扫【全仓】(排除
.venv/.git/node_modules 等)，不靠固定文件名单——固定名单会漏掉没登记的新落点(复核 hunter#2)。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)  # swarm 包目录（含 .env）
_SELF_REL = os.path.join("scripts", "model_swap_audit.py")

# ── ① 权威配置：模型名的单一事实源，下线换装【必须】逐个手改；任何残留 = 硬失败（零豁免）──
AUTHORITATIVE = [
    (".env", "★live 路由(本机权威)★ SWARM_MODEL_ROUTING_* / _FALLBACK / WORKER_PARALLEL_POOL / WORKER_PRIMARY|FALLBACK / BRAIN_*"),
    ("config/settings.py", "无 .env 环境(CI/他人)的默认值：routing_* / *_fallback / worker_parallel_pool / worker_fallback / brain_*"),
    ("setup.sh", "新装 .env 模板（写给全新克隆）"),
    (".env.example", "样例 .env（文档/onboarding）"),
    (".env.docker.example", "Docker 样例 .env"),
    ("models/capability_store.py", "名字启发式 _CONTEXT_HINTS(窗口) / _MULTIMODAL_HINTS(视觉)：新模型名无线索时须显式登记，否则窗口低估/多模态漏判"),
]
# 权威配置文件路径集合：这些文件里出现下线名一律算残留（不接受任何注释豁免）。
_AUTH_PATHS = {p for p, _ in AUTHORITATIVE}

# ── ② 派生/缓存：自动跟随 ① 的配置，无需改模型名，只需触发刷新 ────────────────
DERIVED = [
    ("scripts/e2e_soak_probe.sh", "起跑探活清单——从 get_config() 动态派生(round41 治本)，改 .env 即自动跟随，无需编辑"),
    ("api/static/js/tabs/config.js", "WebUI ‹Worker 子任务路由› 面板：值走 GET /api/routing=get_config()；但 placeholder 示例是硬编码，换装须手改"),
    ("WebUI 面板（运行期）", "读 GET /api/routing = get_config() 缓存；见 ③ 需 restart/reload 才刷新"),
]

# ── ③ 运行期/DB：不是文件，换装后需显式动作 ──────────────────────────────────
RUNTIME_STEPS = [
    "restart-api（scripts/restart-api.sh）：get_config() 是进程内单例缓存，改 .env 后【必须重启或 PUT /api/routing 触发 reload_config()】，否则 WebUI/worker 仍读旧模型名。",
    "★能力库脏行清理（路由安全，非仅显示）★：retired 模型在 model_capabilities 表若有 source=probed 的旧行，会【超过】新配模型(未探=default)胜出，把多模态子任务静默首派到死端点。"
    "代码侧已加 in_use 闸拦截(router._multimodal_model_from_capabilities)，但仍应清行：DELETE /api/models/capabilities?provider_id=<p>&model_id=<name> 或 capability_store.delete_capability(p, name)。upsert-only 探测不自动删旧行。",
    "soak 探活：scripts/e2e_soak_probe.sh 跑一遍确认新模型全绿、每条路由链有健康兜底。",
]

# ── ④ 测试/文档/前端：硬编码断言(须改)或历史示例(可留) ──────────────────────
TESTS_DOCS = [
    ("test/test_routing_local_workers.py", "对 live 默认配置断言（如 complex fallback[0]）——换装后须改断言"),
    ("test/test_capability_store.py", "多模态/窗口启发式断言——新模型若加进 hints 须加断言"),
    ("test/test_elaborate_budget_fix.py", "预算安全下界引用【最小 worker 窗口】——最小窗口变了须调注释/常量"),
    ("brain/nodes/dispatch.py / models/router.py / worker/executor.py / brain/planning_nodes.py", "代码注释里的举例模型名——非功能但会误导审计，顺手换掉"),
    ("docs/MODEL_SWAP_RUNBOOK.md", "本换装流程文档"),
]

# 扫描面 = 活配置/代码/前端；排除【日志/历史文档/数据/生成物】——那里出现下线名是合法记录，非残留。
_SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".pytest_cache",
              ".mypy_cache", ".ruff_cache", "dist", "build", ".idea", ".vscode",
              "cassettes", "checkpoints", "e2e-projects", "archive", "logs",
              "memory", "tool-results", "scratchpad", "htmlcov", ".claude"}
# 只扫这些扩展名（活配置/代码/前端）。故意排除 .md(历史文档)/.log/.jsonl(日志&转录)/
# .json(夹具&cassette 数据)——它们记录历史/运行态，含旧模型名是合法的，不算残留。
_SCAN_EXT = {".py", ".sh", ".js", ".ts", ".tsx", ".jsx", ".vue",
             ".yaml", ".yml", ".toml", ".cfg", ".ini", ".html"}
# 注释里出现这些词 = 历史/换装说明，非活引用（仅对【非权威配置】文件豁免）。
_EXEMPT_RE = re.compile(r"下线|换装|等效|retired|历史|equivalent|旧名|deprecated")


# 只扫【活】env 文件；.env.bak-* / .env.lock 是备份/锁，含旧名是历史，不算残留。
_ENV_FILES = {".env", ".env.example", ".env.docker.example"}


def _scannable(fn: str) -> bool:
    return fn in _ENV_FILES or os.path.splitext(fn)[1].lower() in _SCAN_EXT


def _iter_repo_hits(needle: str):
    """扫活配置/代码/前端找 needle（排除日志/历史文档/数据/生成物）。产出 (rel, lineno, line)。"""
    for root, dirs, files in os.walk(_PKG):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not _scannable(fn):
                continue
            path = os.path.join(root, fn)
            rel = os.path.relpath(path, _PKG)
            if rel == _SELF_REL:
                continue  # 本脚本不自扫（模型名以参数传入，无硬编码残留）
            try:
                with open(path, encoding="utf-8") as fh:
                    for i, line in enumerate(fh, 1):
                        if needle in line:
                            yield rel, i, line.rstrip("\n")
            except (OSError, UnicodeDecodeError):
                continue


def _live_routing_snapshot() -> tuple[set[str], bool]:
    """加载 get_config()（读 .env）打印当前 live 路由。返回 (模型名集合, ok)。
    ok=False → 载不到配置，调用方必须视为【硬失败】，不得据空集判绿。"""
    sys.path.insert(0, _PKG)
    try:
        from swarm.config.settings import get_config
    except Exception as e:  # noqa: BLE001
        print("  ❌ 无法加载 get_config()——请用【.venv/bin/python】在包目录内运行本脚本。")
        print(f"     ({type(e).__name__}: {e})")
        return set(), False
    m = get_config().model
    w = get_config().worker
    tiers = [
        ("brain",       m.brain_primary,     [m.brain_fallback]),
        ("worker",      m.worker_primary,    [m.worker_fallback]),
        ("trivial",     m.routing_trivial,   list(m.routing_trivial_fallback)),
        ("medium",      m.routing_medium,    list(m.routing_medium_fallback)),
        ("complex",     m.routing_complex,   list(m.routing_complex_fallback)),
        ("multimodal",  m.routing_multimodal, list(m.routing_multimodal_fallback)),
    ]
    pool = list(getattr(w, "worker_parallel_pool", []) or [])
    names: set[str] = set()
    print("── live 路由快照（get_config() 读 .env）──")
    for role, primary, fb in tiers:
        print(f"  {role:11} 首选={primary}  备选={fb}")
        names.add(primary)
        names.update(fb)
    print(f"  {'pool':11} worker_parallel_pool={pool}")
    names.update(pool)
    return {n for n in names if n}, True


def main() -> int:
    ap = argparse.ArgumentParser(description="模型下线换装审计器")
    ap.add_argument("--retired", default="", help="逗号分隔的下线模型名，断言已从全仓清除")
    ap.add_argument("--new", default="", help="（可选）新上线模型名，提示下游检查")
    args = ap.parse_args()

    print("=" * 78)
    print("模型下线换装审计")
    print("=" * 78)

    live_names, snap_ok = _live_routing_snapshot()

    print("\n── ① 权威配置落点（下线换装必须手改；残留=硬失败）──")
    for rel, desc in AUTHORITATIVE:
        mark = "✓" if os.path.isfile(os.path.join(_PKG, rel)) else "∅(缺)"
        print(f"  [{mark}] {rel}\n        {desc}")
    print("\n── ② 派生/缓存 ──")
    for rel, desc in DERIVED:
        print(f"  • {rel}\n        {desc}")
    print("\n── ③ 运行期/DB 动作（换装后必做）──")
    for i, step in enumerate(RUNTIME_STEPS, 1):
        print(f"  {i}. {step}")
    print("\n── ④ 测试/文档/前端 ──")
    for rel, desc in TESTS_DOCS:
        print(f"  • {rel} — {desc}")

    rc = 0
    if not snap_ok:
        rc = 2  # 无法核验 live 路由——绝不假绿

    retired = [x.strip() for x in args.retired.split(",") if x.strip()]
    if retired:
        print("\n── 残留检查（全仓活配置/代码/前端；名字在代码/配置值段=残留，仅注释历史说明豁免）──")
        for name in retired:
            hits = list(_iter_repo_hits(name))
            live_hits = []
            for rel, ln, txt in hits:
                # 按 # 切【代码/配置值】与【注释】：名字在代码段 = 活残留（含 js/html 无 # 的整行）；
                # 名字只在注释段且注释含换装说明词 = 历史说明，豁免。避免 hunter#3 整行误豁免。
                code, _, comment = txt.partition("#")
                if name in code:
                    live_hits.append((rel, ln, txt))
                elif _EXEMPT_RE.search(comment):
                    continue
                else:
                    live_hits.append((rel, ln, txt))
            if live_hits:
                rc = 1
                print(f"  ❌ {name} 仍有 {len(live_hits)} 处活引用：")
                for rel, ln, txt in live_hits:
                    tag = "【权威配置】" if rel in _AUTH_PATHS else ""
                    print(f"       {tag}{rel}:{ln}: {txt.strip()[:88]}")
            else:
                extra = f"（另有 {len(hits)} 处注释/历史引用，已豁免）" if hits else ""
                print(f"  ✅ {name} 已从全仓活引用清除{extra}")
            if snap_ok and name in live_names:
                rc = 1
                print(f"  ❌ {name} 仍在 live 路由快照中——检查 .env 是否漏改或 API 未 reload")

    if args.new:
        print(f"\n── 新模型 {args.new} 下游检查 ──")
        low = args.new.lower()
        try:
            from swarm.models import capability_store as cap
            ctx = cap.heuristic_context_window(args.new, kind="local")
            mm = cap.heuristic_supports_multimodal(args.new)
            print(f"  启发式窗口={ctx}  多模态={mm}")
            if not any(h in low for h in cap._MULTIMODAL_HINTS):
                print("  ⚠️ 名字无多模态线索 → 若该模型支持视觉，须把线索加进 _MULTIMODAL_HINTS")
            if ctx == 128_000 and "27b" in low:
                print(f"  ⚠️ 窗口回退到泛匹配值 {ctx}；若标称更大，加进 _CONTEXT_HINTS（须排在泛匹配前）")
        except Exception as e:  # noqa: BLE001
            rc = rc or 2
            print(f"  ❌ 能力启发式检查失败（同上，需 .venv/bin/python）：{e}")

    print("\n" + "=" * 78)
    if rc == 0:
        print("✅ 完成——无残留、无漂移")
    elif rc == 2:
        print("❌ 无法核验（未用 .venv/bin/python 或不在包目录）——本结果不完整，勿据此判绿")
    else:
        print("❌ 有残留/漂移——见上 ❌")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
