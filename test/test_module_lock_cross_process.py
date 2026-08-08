"""#29-4 T-4：`ModuleLock` **跨进程互斥**真连测试 —— 此前在任何环境下零覆盖。

## 为什么这条测试必须存在

`infra/redis_client.py` 的 `ModuleLock` 有两层：
  · 进程内 `threading.Lock`（`_local_held`）—— 同进程同 key 互斥
  · Redis `SET NX`（`_redis_held`）—— **跨进程**互斥

跨进程互斥是它存在的全部理由（并行 worker 是多进程/多副本）。但实测：全仓 23 个碰
redis 的测试文件在三种配置下**结果逐字相同**——

  | 组 | 配置 | 结果 | `get_redis()` |
  |---|---|---|---|
  | A | Redis 可用 + ENABLED=true | 184 passed | calls=15, real=3 |
  | B | ENABLED=false（原 CI 形态） | 184 passed | — |
  | C | ENABLED=true 但端口不可达 | 184 passed | calls=15, real=0 |

原因不是"CI 缺 service 导致降级形态被记绿"（那是 29 号文 T-4 的原推断，已被上表证伪），
而是**它们故意 `monkeypatch get_redis→None`**，因为降级形态才是那些测试的被测对象。
真正的缺口是：`grep multiprocessing|subprocess × ModuleLock` = **空** —— 跨进程互斥
这条命题从来没有任何测试碰过它。

## 区分力从哪来（本测试为什么不是又一条"看着有用"的测试）

Redis 生效时：进程 B 的 `SET NX` 失败 → `_acquire_key_only` 回滚本地锁 → `acquire()` 返 False。
Redis 失效时：两个子进程**各有自己的 `_LOCAL_LOCKS` 字典**（进程隔离）→ 两边都拿到锁
→ 断言"恰好 1 个成功"立刻红。也就是说**本测试在降级形态下必红**，这正是它的价值：
它是全仓唯一一条"Redis 真的连上了、跨进程层真的生效了"的机读证据。

## 为什么用 subprocess 而不是 threading

threading 共享同一个 `_LOCAL_LOCKS`，进程内层就能让"恰好 1 个成功"成立 ⇒ Redis 层
被本地层**背书**，突变掉 Redis 层测试仍绿（血规 10②：冗余防御互相兜底=两条都不可
证伪）。必须真起进程。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 子进程脚本：抢锁 → 汇报结果。**必须打印 _redis_held**，否则"经 Redis 拿到"与
# "经进程内兜底拿到"在输出上不可分（血规 10④：缺席必须机读可辨）。
_CHILD = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, %(root)r)
    import swarm.infra.redis_client as rc

    key = os.environ["LOCK_KEY"]
    barrier_at = float(os.environ["BARRIER_AT"])

    lock = rc.ModuleLock("%(proj)s", key, ttl_sec=60)
    # 齐步走：所有子进程在同一墙钟时刻发起 acquire，构造真争用
    while time.time() < barrier_at:
        time.sleep(0.002)
    ok = lock.acquire()
    out = {
        "pid": os.getpid(),
        "acquired": bool(ok),
        "redis_held": bool(getattr(lock, "_redis_held", False)),
        "local_held": bool(getattr(lock, "_local_held", False)),
    }
    print("RESULT " + json.dumps(out), flush=True)
    if ok:
        # 默认**不 release**：并发臂需要赢家一直持锁，否则先跑完的进程放锁后另一个
        # 进程可能"接着拿到"，两个都报 acquired=True 却并非并发双持（时序假绿）。
        time.sleep(float(os.environ.get("HOLD_SEC", "2")))
        # RELEASE=1 时显式释放：串行臂（release→下一个能拿到）必须走真 release 路径。
        # ★进程直接退出【不等于】release★：退出只让 Redis key 靠 TTL 回收（那是给崩溃
        # 兜底的正确行为），下一个进程在 TTL 内照样拿不到。第一版本测试正是漏了这点，
        # 把"我没调 release"误报成"生产锁泄漏"。
        if os.environ.get("RELEASE") == "1":
            lock.release()
            print("RELEASED", flush=True)
""")


def _run_children(n: int, lock_key: str, *, env_extra: dict | None = None,
                  hold_sec: float = 2.0) -> list[dict]:
    """起 n 个子进程同时抢同一把锁，回收各自结果。"""
    script = _CHILD % {"root": str(ROOT), "proj": "p-xproc"}
    barrier_at = time.time() + 1.5      # 给子进程留足 import 时间
    env = dict(os.environ)
    env.update({
        "LOCK_KEY": lock_key,
        "BARRIER_AT": f"{barrier_at}",
        "HOLD_SEC": f"{hold_sec}",
        # 子进程必须真连 Redis（父进程的 .env 已提供 URI）
        "SWARM_REDIS_ENABLED": "true",
    })
    if env_extra:
        env.update(env_extra)

    procs = [
        subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(n)
    ]
    results: list[dict] = []
    for p in procs:
        try:
            out, err = p.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            pytest.fail(f"子进程超时未返回。stderr={err[-2000:]}")
        lines = [ln for ln in out.splitlines() if ln.startswith("RESULT ")]
        if not lines:
            pytest.fail(
                "子进程没有输出 RESULT 行 —— 它在 acquire 之前就挂了，"
                f"这不是「抢不到锁」而是测试基建坏了。\nstdout={out[-1500:]}\nstderr={err[-2000:]}")
        results.append(json.loads(lines[-1][len("RESULT "):]))
    return results


@pytest.fixture
def _redis_ready(require_svc):
    """真连 Redis；缺席按 SWARM_TEST_REQUIRE_SERVICES 硬失败或可见 skip。

    ★判定放在 fixture（测试体阶段）而非 `@skipif(...)` 实参★——后者是 collection 期
    求值，正是 T-7 要治的病。
    """
    require_svc("redis")


def _fresh_key(tag: str) -> str:
    """每次用全新 key —— 复用 key 会被上一轮遗留的 Redis 条目（靠 TTL 回收）影响。"""
    return f"xproc-{tag}-{uuid.uuid4().hex[:12]}"


def test_cross_process_mutex_exactly_one_winner(_redis_ready):
    """★核心命题★ 4 个进程同时抢同一 ModuleLock key → **恰好 1 个**成功。

    这是 ModuleLock 存在的全部理由，此前零覆盖。
    """
    key = _fresh_key("mutex")
    results = _run_children(4, key, hold_sec=3.0)

    winners = [r for r in results if r["acquired"]]
    assert len(results) == 4, f"应回收 4 份结果，实得 {len(results)}"
    assert len(winners) == 1, (
        f"跨进程互斥失效：{len(winners)}/4 个进程同时拿到同一把锁 {key!r}。\n"
        f"全部结果={results}\n"
        "若 winners 为 4 且各自 redis_held=False ⇒ 跑的是**进程内降级形态**"
        "（每进程各有独立 _LOCAL_LOCKS，谁都不互斥）——那说明 Redis 没真连上，"
        "本测试正是为区分这两种情形而存在。"
    )
    # ★关键★ 赢家必须是**经 Redis**拿到的：若 redis_held=False 而只有一个赢家，
    # 那只能是巧合（比如另外三个进程崩了），不是跨进程互斥生效。
    assert winners[0]["redis_held"] is True, (
        f"唯一赢家的 _redis_held=False ⇒ 它是靠进程内兜底拿到的，跨进程层未生效。"
        f"结果={results}")


def test_cross_process_different_keys_do_not_block(_redis_ready):
    """反面：不同 module key 之间**不得**互斥（否则并行度被锁死成串行）。

    没有这一条，把 acquire 改成"永远只有第一个成功"也能让上一条测试绿——
    上一条只证了"不会多于 1 个"，这一条证"不会少于应有的并行度"。
    """
    keys = [_fresh_key(f"par{i}") for i in range(3)]
    script = _CHILD % {"root": str(ROOT), "proj": "p-xproc"}
    barrier_at = time.time() + 1.5
    procs = []
    for k in keys:
        env = dict(os.environ)
        env.update({"LOCK_KEY": k, "BARRIER_AT": f"{barrier_at}", "HOLD_SEC": "2",
                    "SWARM_REDIS_ENABLED": "true"})
        procs.append(subprocess.Popen([sys.executable, "-c", script], cwd=str(ROOT),
                                      env=env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True))
    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        lines = [ln for ln in out.splitlines() if ln.startswith("RESULT ")]
        assert lines, f"子进程无 RESULT 行\nstdout={out[-1000:]}\nstderr={err[-1500:]}"
        results.append(json.loads(lines[-1][len("RESULT "):]))

    assert all(r["acquired"] for r in results), (
        f"不同 module key 互相阻塞了 —— 并行度被锁死。结果={results}")
    assert all(r["redis_held"] for r in results), (
        f"有进程未经 Redis 持锁 ⇒ 跑的是降级形态，本测试无意义。结果={results}")


def test_cross_process_release_lets_next_acquire(_redis_ready):
    """A `release()` 之后，**另一个进程**必须能拿到同一把锁。

    ★为什么这条不可省★：只断"恰好一个赢"时，把 `release` 的 Redis DEL 改坏不会红——
    锁泄漏是本项目已实证的真事故面（`_release_key_only` 里那条 WARNING 就是为它加的：
    DEL 失败 ⇒ key 留到 TTL ⇒ 其它任务整个 TTL 内都拿不到该模块）。

    ★诚实边界★：本条验的是 `release()` **被调用后**跨进程可再获取。进程**崩溃**（不调
    release）后靠 TTL 回收是另一条命题，本测试不覆盖（TTL 60s，秒级测试验不了；
    要验须把 ttl_sec 调到 ~2s 并等待，属可加项，已登记）。
    """
    key = _fresh_key("serial")
    first = _run_children(1, key, hold_sec=0.05, env_extra={"RELEASE": "1"})
    assert first[0]["acquired"] and first[0]["redis_held"], f"第一轮就没拿到: {first}"
    second = _run_children(1, key, hold_sec=0.05, env_extra={"RELEASE": "1"})
    assert second[0]["acquired"], (
        f"前一持有者已 release()，后来者仍拿不到锁 {key!r} ⇒ Redis DEL 没生效，"
        f"key 留到 TTL 才回收（此间该模块对所有任务不可用）。结果={second}")
    assert second[0]["redis_held"] is True, (
        f"后来者拿到了但不是经 Redis ⇒ 跨进程层未生效。结果={second}")


def test_degraded_form_is_detectable(_redis_ready):
    """自证本测试**有区分力**：强制关掉 Redis 后，4 个进程应【全部】拿到锁。

    ★这条是上面三条的元测试★（血规 10②「把机制整块删掉，测试会不会红」的**正向**写法）：
    它证明"恰好 1 个赢"不是任何环境下的恒真命题，而是**真的由 Redis 跨进程层产生的**。
    若本条也只有 1 个赢家，说明有别的东西在提供互斥（比如测试串行执行），
    上面三条的绿就都是别人背书的。
    """
    key = _fresh_key("degraded")
    results = _run_children(4, key, env_extra={"SWARM_REDIS_ENABLED": "false"},
                            hold_sec=2.0)
    winners = [r for r in results if r["acquired"]]
    assert len(winners) == 4, (
        f"关掉 Redis 后本应【全部】拿到（各进程独立 _LOCAL_LOCKS，无跨进程互斥），"
        f"实际 {len(winners)}/4。这意味着互斥来源不是 Redis —— 上面三条测试的绿"
        f"是被别的机制背书的，需重新设计。结果={results}")
    assert all(not r["redis_held"] for r in results), (
        f"SWARM_REDIS_ENABLED=false 时仍有进程 redis_held=True，环境未真正隔离。结果={results}")
