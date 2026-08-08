"""#29-4 T-6：测试用户清理的**失败可见性**。

治的是 `test/conftest.py::_purge_test_users` 两层裸吞异常（`except: return` /
`except: pass`）：清理失败零日志零账目，于是三种情形在输出上完全不可分——
  ① 清理成功（删了 N 个）· ② 无 PG 依赖，合法跳过 · ③ 连上了但 DELETE 失败
③ 的后果是垃圾测试用户在 `.env` 指向的**真库**里持续累积，而没有任何信号。

本文件是那本机读账的**消费者**——账目没有消费者等于没造（血规 10④）。
"""
from __future__ import annotations

import logging
import os

import pytest


def test_service_absent_summary_visible_under_quiet_flags():
    """★#29-4 T-7 的可观测性锁★ 服务缺席时，汇总行必须在**本仓惯用命令**下真的可见。

    起真 pytest 子进程（`-p no:warnings -q`，与 CI/本地一致），把 PG 指向死端口。

    ★这条测试的来由（值得留痕）★
    我的第一版汇总用 session 级 autouse fixture 打 `logging.warning`，然后**直接宣称
    "缺席可观测"** —— 实测那条 WARNING 在 `-p no:warnings -q` 下根本不显示（pytest 只在
    失败时吐 captured log）。造了机制却没验证它到得了用户眼前，正是本批要治的
    "降级不可观测"本身。改用 `pytest_terminal_summary` 后才真的可见。
    没有这条测试，"可见"这件事就只由我某次手敲命令背书 —— 下一次改动把它弄坏不会有人知道。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["SWARM_DB_POSTGRES_URI"] = "postgresql://postgres:postgres@127.0.0.1:5499/swarm"
    env.pop("SWARM_TEST_REQUIRE_SERVICES", None)   # 走"可见 skip"档，不是硬失败档
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "test/test_create_project_conflict.py",
         "-p", "no:warnings", "-q"],
        cwd=str(root), env=env, capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    assert "SERVICE_ABSENT" in out, (
        "服务缺席时汇总行在 `-p no:warnings -q` 下不可见 ⇒ "
        "「整批降级为 skip」与「全部真跑」在终端输出上不可分（降级不可观测）。\n"
        f"实际输出尾部:\n{out[-1200:]}")
    assert "已降级为 skip" in out, (
        f"汇总行必须说明后果（已降级、非真跑），实际输出:\n{out[-800:]}")


def test_service_present_emits_no_absent_noise():
    """反向：服务正常时**不得**出现该汇总行。

    没有这一条，把汇总改成"恒发"也能让上一条绿——而恒发的告警等于没有告警
    （本仓已实证 always-emit 防粘滞的反面：粘滞噪声会让人学会忽略它）。
    """
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    p = subprocess.run(
        [sys.executable, "-m", "pytest", "test/test_create_project_conflict.py",
         "-p", "no:warnings", "-q"],
        cwd=str(root), env=dict(os.environ), capture_output=True, text=True, timeout=300)
    out = p.stdout + p.stderr
    if "SERVICE_ABSENT" in out:
        pytest.skip("本机 PG 不可用，本条（服务正常时无噪声）无法验证")
    assert "SERVICE_ABSENT" not in out


def _run_pytest(args: list[str], env_extra: dict | None = None) -> tuple[int, str]:
    """起真 pytest 子进程（惯用命令形态），回 (rc, 合并输出)。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env.pop("SWARM_TEST_REQUIRE_SERVICES", None)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, "-m", "pytest", *args, "-p", "no:warnings", "-q"],
                       cwd=str(root), env=env, capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


_DEAD_PG = "postgresql://postgres:postgres@127.0.0.1:5499/swarm"


def test_hard_fail_summary_does_not_claim_skip():
    """★复核 M-3 整改锁★ 硬失败档的汇总行绝不能说"已降级为 skip"。

    原实现 fail 与 skip 两档共用一个集合、文案只描述 skip 档 ⇒ 在
    `SWARM_TEST_REQUIRE_SERVICES=1`（CI，也是这机制**唯一为之设计**的环境）里，
    用例是 ERROR 却被汇总成"已降级为 skip"，还建议"应置 SWARM_TEST_REQUIRE_SERVICES=1"
    —— 那开关已经置了。机制在它唯一的目标环境里给误导信息。
    """
    rc, out = _run_pytest(
        ["test/test_create_project_conflict.py"],
        {"SWARM_DB_POSTGRES_URI": _DEAD_PG, "SWARM_TEST_REQUIRE_SERVICES": "1"})
    assert rc != 0, f"硬失败档必须非零退出。输出:\n{out[-800:]}"
    assert "hard-fail" in out, f"缺硬失败档汇总行。输出:\n{out[-800:]}"
    assert "已硬失败" in out, f"汇总必须说明是硬失败。输出:\n{out[-800:]}"
    assert "已降级为 skip" not in out, (
        "硬失败档的汇总说了「已降级为 skip」——用例其实是 ERROR，这是误导信息。\n"
        f"输出:\n{out[-900:]}")


def test_needs_service_without_name_is_fail_closed(tmp_path):
    """★复核 M-4 整改锁★ `@needs_service` 漏写服务名必须让用例**红**，不是照跑。

    名字写**错**本就 fail-closed（ValueError），名字**缺失**原先却 fail-open
    （`for name in mark.args` 空集不执行）—— 方向相反，而漏参数比拼错常见得多。

    实现用 `pytest.fail`。`pytest.UsageError` 实测**同样有效**（`pytest_runtest_setup`
    里抛它会让用例 ERROR、rc≠0），两者在本条断言下等价 —— 故用哪个都行，本条守的是
    "漏名必须红"这件事本身，不锚定实现细节。

    ★留痕：我一度写下"UsageError 会被 pytest 吞掉、用例仍 PASSED"并据此说本条测试
    "逮到了我的第二个 fail-open"——那是**编造的因果**。突变实验里 UsageError 版仍绿，
    我去实跑才发现它根本没被吞（照样 ERROR）。"仍绿"的正确解释是**突变前后行为等价**
    （不是有效突变），不是测试没牙。归因动词用前必须有指向施动者的证据。★
    """
    from pathlib import Path
    probe = Path(__file__).resolve().parent / "test_zz_tmp_needs_service_probe.py"
    probe.write_text(
        "import pytest\n\n"
        "@pytest.mark.needs_service\n"          # ← 故意漏参数
        "def test_should_not_run():\n"
        "    assert True\n",
        encoding="utf-8")
    try:
        rc, out = _run_pytest([f"test/{probe.name}"])
    finally:
        probe.unlink(missing_ok=True)
    assert rc != 0, (
        "`@needs_service` 漏写服务名时用例照旧 PASSED ⇒ 闸静默不设（fail-open）。\n"
        f"输出:\n{out[-900:]}")
    assert "没写服务名" in out, f"失败信息应指明漏写服务名。输出:\n{out[-900:]}"


def test_purge_failure_surfaces_in_terminal_summary():
    """★自查发现（#29-4）整改锁★ 会话末**真跑**那次清理失败，必须在终端可见。

    `_PURGE_LEDGER` 原先只被本文件里"驱动出来的"清理读到；**会话末 autouse fixture
    真跑那一次**的账目没有任何消费者，唯一出口是 `logger.warning`——而它在
    `-p no:warnings -q` 下不显示。于是 T-6 要治的病（清理静默失败 ⇒ 垃圾用户在真库里
    累积而无人知晓）在 T-6 的修复里原样存活。**账造好了没有消费者＝没造。**

    验法：子进程里 patch 掉 psycopg.connect 让清理必失败，断汇总段出现 PURGE_FAILED。
    """
    from pathlib import Path
    probe = Path(__file__).resolve().parent / "test_zz_tmp_purge_fail_probe.py"
    probe.write_text(
        '"""临时探针：让会话末清理必失败，验汇总段可见。"""\n'
        "import psycopg\n\n"
        "def test_break_purge_then_pass():\n"
        "    def _boom(*a, **k):\n"
        '        raise psycopg.OperationalError("PROBE: 故意让清理失败")\n'
        "    psycopg.connect = _boom      # 不还原：会话末清理正需要它坏着\n"
        "    assert True\n",
        encoding="utf-8")
    try:
        rc, out = _run_pytest([f"test/{probe.name}"])
    finally:
        probe.unlink(missing_ok=True)
    assert "PURGE_FAILED" in out, (
        "会话末清理失败没有在终端汇总里出现 ⇒ 垃圾测试用户在真库累积而无信号。\n"
        f"rc={rc} 输出:\n{out[-1200:]}")
    assert "累积" in out, f"汇总必须说明后果。输出:\n{out[-900:]}"


def test_purge_ledger_records_success_or_skip(purge_probe, caplog):
    """跑真清理 → 账目必须落到三个已知相位之一，且与日志一致。

    本条不假设本机有没有 PG：两种结果都合法，但**必须留痕**。
    """
    purge, ledger = purge_probe
    with caplog.at_level(logging.INFO, logger="swarm.test"):
        purge()
    assert ledger["phase"] in ("done", "skipped", "failed"), (
        f"清理后 phase={ledger['phase']!r} —— 三个相位之外的取值说明有分支没记账")
    msgs = [r.getMessage() for r in caplog.records if "[PURGE]" in r.getMessage()]
    if ledger["phase"] == "done":
        # 删 0 个是常态（干净库），此时不强制要求日志；删了就必须有 INFO
        assert isinstance(ledger["deleted"], int), "done 相位必须带 deleted 计数"
        if ledger["deleted"]:
            assert msgs, "删了用户却没有任何 [PURGE] 日志"
    else:
        assert msgs, f"phase={ledger['phase']} 必须留痕，实际零 [PURGE] 日志"


def test_purge_failure_is_loud_and_accounted(purge_probe, caplog, monkeypatch):
    """★核心区分力★ DB 层抛错时：必须 WARNING + phase=failed，且**不得**外抛。

    这条是原病灶的正向验证：突变前（裸 `except: pass`）本条必红。
    """
    purge, ledger = purge_probe

    import psycopg

    def _boom(*a, **k):
        raise psycopg.OperationalError("MUTATED: 模拟 DELETE 被拒/连接断开")

    monkeypatch.setattr(psycopg, "connect", _boom)

    with caplog.at_level(logging.WARNING, logger="swarm.test"):
        purge()          # 绝不许外抛——会话已结束，抛出只会掩盖真实测试结果

    assert ledger["phase"] == "failed", (
        f"DB 抛错后 phase={ledger['phase']!r}，应为 'failed'")
    assert "MUTATED" in str(ledger["error"]), (
        f"账目里必须带失败原因原文，实际 error={ledger['error']!r}")
    warns = [r.getMessage() for r in caplog.records
             if r.levelno >= logging.WARNING and "[PURGE]" in r.getMessage()]
    assert warns, (
        "清理失败必须至少一条 WARNING（降级路径可观测铁律）——"
        "静默失败会让垃圾用户在真库里持续累积且无人知晓")
    assert any("累积" in m for m in warns), (
        f"WARNING 必须说明后果（垃圾用户累积），实际={warns}")


def test_purge_skip_is_distinguishable_from_success(purge_probe, monkeypatch, caplog):
    """「没连库」与「连了库但一个都没删」必须可分。

    ★这条是本文件真正的承重条★：两者的用户可见结果都是"什么也没发生"，
    若只记 `deleted=0` 便无法区分——而它们的含义完全不同（前者=清理机制根本没跑，
    后者=库是干净的）。
    """
    purge, ledger = purge_probe

    # 造"取不到 PG 配置"的情形
    import swarm.config.settings as st

    def _boom_cfg(*a, **k):
        raise RuntimeError("MUTATED: 无 PG 配置")

    monkeypatch.setattr(st, "DatabaseConfig", _boom_cfg)
    with caplog.at_level(logging.INFO, logger="swarm.test"):
        purge()
    assert ledger["phase"] == "skipped", (
        f"无 PG 配置时 phase 应为 'skipped'，实际 {ledger['phase']!r}")
    assert ledger["deleted"] is None, (
        "skipped 相位的 deleted 必须是 None 而非 0 —— 0 会与「库是干净的」混淆，"
        f"实际 {ledger['deleted']!r}")
