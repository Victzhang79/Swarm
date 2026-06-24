#!/usr/bin/env python3
"""本地 worker 模型并发探针：按 worker 真实路径(router + 流式)压不同并发，找安全并发上限。

度量每个并发档：成功率、TTFT(首 token 延迟)、总时长、是否出现 >STALL_S 的 chunk 间隔
（=worker 的 stream_chunk_timeout 触发点，E2E 超时真因）。据此定 SWARM_WORKER_MAX_CONCURRENT。
"""
from __future__ import annotations
import asyncio
import sys
import time

sys.path.insert(0, ".")
from swarm.models.router import ModelRouter

MODEL = "Qwopus3.6-27B-v2-NVFP4"
LEVELS = [4, 8, 12]
STALL_S = 45.0          # worker stream_chunk_timeout 阈值
REQ_TIMEOUT = 300.0     # 单请求硬超时（长生成）
MAX_TOKENS = 4096       # 贴近 worker_max_tokens=8192 的长解码负载
# 贴近 worker 真实负载：大上下文(系统约束) + 长解码(整套多文件实现)
PROMPT = (
    "你是资深 Java 工程师。请实现一个完整的 RuoYi 风格告警模块，必须输出【完整可编译代码】：\n"
    "1) Controller：AlarmAppController，含 list/getInfo/add/edit/remove 五个 CRUD 接口，"
    "带 @PreAuthorize 权限注解、@RestController、完整 import；\n"
    "2) Service 接口 IAlarmAppService + 实现 AlarmAppServiceImpl（注入 Mapper、实现全部方法）；\n"
    "3) Mapper 接口 AlarmAppMapper（@Mapper）；\n"
    "4) Entity AlarmApp（继承 BaseEntity，含 appId/appName/appSecret/status/createTime 字段、getter/setter）。\n"
    "每个类都要完整 package 声明、全部 import、完整方法体。逐个类输出，不要省略。"
)


async def one_call(llm, idx: int) -> dict:
    t0 = time.monotonic()
    ttft = None
    last = t0
    max_gap = 0.0
    chunks = 0
    err = None
    try:
        async for ch in llm.astream(PROMPT):
            now = time.monotonic()
            if ttft is None:
                ttft = now - t0
            gap = now - last
            if gap > max_gap:
                max_gap = gap
            last = now
            chunks += 1
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:60]}"
    return {"ok": err is None and chunks > 0, "ttft": ttft, "total": time.monotonic() - t0,
            "max_gap": max_gap, "chunks": chunks, "err": err}


def pct(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return vals[min(len(vals) - 1, int(len(vals) * p))]


async def run_level(n: int) -> None:
    router = ModelRouter()
    llm = router.get_model_by_name(MODEL, temperature=0.2)
    try:
        llm = llm.bind(max_tokens=MAX_TOKENS)
    except Exception:  # noqa: BLE001
        pass
    tasks = [asyncio.wait_for(one_call(llm, i), timeout=REQ_TIMEOUT + 30) for i in range(n)]
    res = []
    for r in await asyncio.gather(*tasks, return_exceptions=True):
        res.append(r if isinstance(r, dict) else {"ok": False, "ttft": None, "total": None,
                   "max_gap": 0, "chunks": 0, "err": f"{type(r).__name__}"})
    ok = sum(1 for r in res if r["ok"])
    stalled = sum(1 for r in res if r["max_gap"] > STALL_S)
    errs = [r["err"] for r in res if r["err"]]
    print(f"[并发 {n:>2}] ok={ok}/{n} | TTFT p50={pct([r['ttft'] for r in res],0.5)} "
          f"max={pct([r['ttft'] for r in res],0.99)} | total p50={pct([r['total'] for r in res],0.5)} "
          f"max={pct([r['total'] for r in res],0.99)} | max_chunk_gap={max((r['max_gap'] for r in res),default=0):.1f}s "
          f"| >{STALL_S:.0f}s停顿={stalled} | 错误={errs[:2]}")
    sys.stdout.flush()


async def main():
    print(f"探针 model={MODEL} stall阈值={STALL_S}s req超时={REQ_TIMEOUT}s")
    for n in LEVELS:
        await run_level(n)
        await asyncio.sleep(3)  # 档间稍歇，让服务回稳


if __name__ == "__main__":
    asyncio.run(main())
