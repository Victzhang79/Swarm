#!/usr/bin/env python3
"""R65D-T5 plan 注入端提交器 —— 把录制 cassette 作为新任务提交，跳过云端规划直入执行。

用途（执行期编排 bug 的零云端复现回路）：
    1) 服务端开闸：.env 加 SWARM_PLAN_INJECT_ENABLE=1 后 restart-api；
       如需连执行期条件性云端 brain 调用（HANDLE_FAILURE 故障分析/L2 LLM 复核）也闸死，
       再加 SWARM_BRAIN_OFFLINE=1（各调用点走既有降级路径，全程零云端）。
    2) 项目基线重置到录制 commit（base_commit 一致性闸 fail-closed）：
       E2E 用 scripts/e2e_reset_baseline.sh；手工则 git reset --hard <cassette.base_commit>。
    3) 提交：
       .venv/bin/python scripts/plan_inject_submit.py cassettes/<task_id>.json \
           --project <project_id> [--api http://127.0.0.1:8420] [--description "..."]

token 取 ~/.swarm/cli_token（e2e 账号，见 scripts/e2e_login.sh——绝不用 admin）。
runner 侧会对 cassette 重跑确定性收尾器（#61 考卷同源 + #57 消费边）得治后形态，
绝不原样回放旧 plan；深校验失败任务直接 FAILED 并落机读 error（plan_inject_*）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description="提交 plan 注入任务（跳过云端规划直入 DISPATCH）")
    ap.add_argument("cassette", help="scripts/cassette_extract.py 抽出的 cassette JSON 路径")
    ap.add_argument("--project", required=True, help="目标 project_id")
    ap.add_argument("--api", default="http://127.0.0.1:8420", help="API base（默认 :8420）")
    ap.add_argument("--description", default=None,
                    help="任务描述（默认取 cassette.task_description）")
    ap.add_argument("--token", default=None,
                    help="Bearer token（默认读 ~/.swarm/cli_token）")
    args = ap.parse_args()

    cassette = json.loads(Path(args.cassette).read_text(encoding="utf-8"))
    desc = (args.description or cassette.get("task_description") or "").strip()
    if not desc:
        print("✗ 无任务描述（cassette.task_description 为空且未传 --description）", file=sys.stderr)
        return 2

    token = args.token or ""
    if not token:
        tok_path = Path.home() / ".swarm" / "cli_token"
        token = tok_path.read_text(encoding="utf-8").strip() if tok_path.exists() else ""
    if not token:
        print("✗ 无 token：先 scripts/e2e_login.sh（绝不用 admin 账号跑 E2E）", file=sys.stderr)
        return 2

    body = json.dumps({
        "description": desc,
        "force": True,          # 注入调试轮常与既往任务同描述，跳过去重
        "auto_accept": True,    # 注入本就跳过 confirm；终态审核也无人值守
        "injected_plan": cassette,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{args.api}/api/projects/{args.project}/tasks",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        print(f"✗ 提交失败 HTTP {exc.code}: {detail}", file=sys.stderr)
        if exc.code == 403:
            print("  提示：服务端需 SWARM_PLAN_INJECT_ENABLE=1 且 restart-api（进程缓存）",
                  file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # 猎手整改：连接拒绝/超时/DNS 失败不许裸 traceback——保持 ✗ 约定与退出码
        print(f"✗ 提交失败（连不上 {args.api}）: {exc}", file=sys.stderr)
        print("  提示：核对 --api 端口与 API 进程是否在跑（restart-api 后再试）", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"✗ 响应不是 JSON（--api 可能指向了错误服务/反代错误页）: {exc}", file=sys.stderr)
        return 1

    task = out.get("task") or {}
    print(f"✓ 注入任务已提交: task={task.get('id')} status={out.get('status') or task.get('status')}")
    print(f"  源录制: task={cassette.get('task_id')} base={str(cassette.get('base_commit'))[:12]} "
          f"subtasks={len((cassette.get('plan') or {}).get('subtasks') or [])}")
    print("  盯法: swarm.log 应见 [PLAN-INJECT] 两行（治后形态重推导 + next=dispatch），"
          "任务状态直接进入执行期；若 FAILED 查 task.error 的 plan_inject_* 机读码")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
