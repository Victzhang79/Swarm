#!/usr/bin/env python3
"""合成**非 Maven** plan cassette 生成器（B-0 末件，27 号文 §7）。

## 为什么需要它

`cassettes/` 里 22 份 plan 快照 **100% Maven**（27 号文 §4.3 实测），于是
`cassette_replay.py` 这条排障回路对异栈**零证明力且静默通过**——B-2~B-6 每批都能跑出
一片绿，跑的全是 Maven 路径。真实非 Maven E2E 基线项目（用户已降优先级）到位之前，
先用合成 cassette 把回路填上。

## 为什么是脚本而不是直接丢 JSON

`cassettes/` 已 gitignore（录像不入库），直接丢的 JSON 会随清理消失、也无从复现。
本脚本入库 → 谁都能一条命令重新生成，且工程树形状与测试**共用同一份 builders**
（`test/stack_workspaces.py`）——单一事实源，不会各写一份漂移。

## 合成 ≠ 现场（诚实边界）

run18/run19 是真实 live 现场固化，**本脚本产出的不是**。它把 RUN19/round62 的**结构**
（多写者争抢根聚合清单 + 模块清单脚手架 + 一批源码挤进单发路径）平移到 npm/go 布局。
JSON 的 `task_description` 与 `_synthetic` 键都会写明这一点，别让后人拿它当现场证据。

## 用法

    python scripts/cassette_synth_non_maven.py                 # 生成到 cassettes/
    python scripts/cassette_replay.py cassettes/synthetic-npm-workspaces.json
    python scripts/cassette_replay.py cassettes/synthetic-go-work.json

工程树与 JSON 同名目录并列落盘（`cassettes/synthetic-npm-workspaces/`），因为 replay 会
读磁盘做 aggregate-vs-新建分流与模板取证——`project_path` 必须指向真实存在的树。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent                      # .../swarm/swarm
_REPO_PARENT = _PKG_ROOT.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))

# 工程树 builders 与测试共用（单一事实源，绝不在此另写一份树）。
_SW = _PKG_ROOT / "test" / "stack_workspaces.py"
_spec = importlib.util.spec_from_file_location("stack_workspaces", _SW)
sw = importlib.util.module_from_spec(_spec)
sys.modules["stack_workspaces"] = sw
_spec.loader.exec_module(sw)

SCHEMA = "swarm-plan-cassette/v1"
_SYNTHETIC_NOTE = (
    "★合成夹具（非真实 E2E 现场）★ run18/run19 是 live 现场固化，本条不是。"
    "把 RUN19/round62 的结构平移到非 Maven 布局，用于给离线重放回路补异栈证明力。"
)


def _plan_dict(subtasks: list[dict], groups: list[list[str]], contract: dict) -> dict:
    return {"subtasks": subtasks, "parallel_groups": groups, "shared_contract": contract}


def _st(sid: str, desc: str, create: list[str], writable: list[str]) -> dict:
    return {"id": sid, "description": desc, "difficulty": "medium", "modality": "text",
            "depends_on": [], "acceptance_criteria": ["ok"],
            "scope": {"create_files": create, "writable": writable, "readable": []}}


def _npm(root: Path) -> dict:
    """npm workspaces：两个子任务都注册进根 package.json（R-1 的 npm 侧：非加性覆盖）。"""
    sw.build_workspace("npm_workspaces", root)
    contract = {"dependencies": [
        {"module": "alarm", "artifacts": ["axios"]},
        {"module": "notify", "artifacts": ["alarm"]},     # 内部包 → workspace:*
    ]}
    plan = _plan_dict([
        _st("st-1", "新建 packages/alarm 包并注册进根 package.json 的 workspaces；"
                    "实现告警规则/派发三个 TypeScript 源文件",
            ["packages/alarm/src/index.ts", "packages/alarm/src/rule.ts",
             "packages/alarm/src/dispatch.ts"], ["package.json"]),
        _st("st-2", "新建 packages/notify 包并注册进根 package.json 的 workspaces",
            ["packages/notify/src/index.ts"], ["package.json"]),
    ], [["st-1", "st-2"]], contract)
    return {"schema": SCHEMA, "task_id": "synthetic-npm-workspaces", "thread_id": None,
            "project_id": "b0-synthetic", "project_path": str(root), "base_commit": None,
            "plan": plan, "shared_contract": contract,
            "file_plan": [{"module": "alarm", "path": "packages/alarm/src/index.ts"},
                          {"module": "notify", "path": "packages/notify/src/index.ts"}],
            "task_description": "npm workspaces 多写者争抢根 package.json",
            "_synthetic": _SYNTHETIC_NOTE}


def _go(root: Path) -> dict:
    """go.work：两个子任务都注册进 go.work（R-1 的 go 侧：曾判死却无人收敛）。"""
    sw.build_workspace("go_work", root)
    contract = {"dependencies": [
        {"module": "billing", "artifacts": ["github.com/gin-gonic/gin"]},
        {"module": "report", "artifacts": ["billing"]},   # 内部模块 → replace
    ]}
    plan = _plan_dict([
        _st("st-1", "新建 billing 模块（go.mod + use 注册进 go.work）；实现 handler/repo/model",
            ["billing/handler.go", "billing/repo.go", "billing/model.go"], ["go.work"]),
        _st("st-2", "新建 report 模块并 use 注册进 go.work", ["report/handler.go"], ["go.work"]),
    ], [["st-1", "st-2"]], contract)
    return {"schema": SCHEMA, "task_id": "synthetic-go-work", "thread_id": None,
            "project_id": "b0-synthetic", "project_path": str(root), "base_commit": None,
            "plan": plan, "shared_contract": contract,
            "file_plan": [{"module": "billing", "path": "billing/handler.go"},
                          {"module": "report", "path": "report/handler.go"}],
            "task_description": "go.work 多写者争抢根 go.work（R-1 死锁现场）",
            "_synthetic": _SYNTHETIC_NOTE}


_BUILDERS = {"synthetic-npm-workspaces": _npm, "synthetic-go-work": _go}


def main() -> int:
    ap = argparse.ArgumentParser(description="生成非 Maven 合成 plan cassette")
    ap.add_argument("--out-dir", default=str(_PKG_ROOT / "cassettes"))
    ap.add_argument("--only", choices=sorted(_BUILDERS), help="只生成其中一条")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    names = [args.only] if args.only else sorted(_BUILDERS)

    for name in names:
        tree = out_dir / name
        if tree.exists():
            shutil.rmtree(tree)      # 幂等重生成：树必须与 JSON 同一批，绝不半新半旧
        tree.mkdir(parents=True)
        cassette = _BUILDERS[name](tree)
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(cassette, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        print(f"[synth] {path}  （工程树 {tree}）")
    print("\n重放：python scripts/cassette_replay.py "
          f"{out_dir.name}/{names[0]}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
