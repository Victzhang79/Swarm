"""B5 worker 基础设施深读治本（DR-05-F1..F8 = #85-92 + #116-B1）行为级测试。"""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────── F7/#86 命令黑名单 rm 变体 ───────────────────────────

def test_86_rm_variants_blocked():
    from swarm.config.command_blacklist_store import _DEFAULT_RULES
    rule = next(p for p, d in _DEFAULT_RULES if "递归删除根" in d)
    rx = re.compile(rule)
    for c in ["rm -rf /", "rm -fr /", "rm -Rf /", "rm --recursive --force /",
              "rm --recursive --force ~", "rm -rf /etc", "rm -rf /usr /var",
              "rm -rf $HOME", "rm -rf /*", "rm -rf /home",
              # ★对抗复核 hunter CONFIRMED HIGH 的 flag 分离/长选项/引号 绕过变体★
              "rm -f -r /", "rm --force --recursive /", "rm -v -r -f /",
              "rm -rf -- /", "rm -rf --no-preserve-root /", 'rm -rf "/"',
              "rm -rf '/'", "rm -rf ~root", "rm -rf /etc/"]:
        assert rx.search(c), f"应拦截: {c}"


def test_86_legit_rm_allowed():
    from swarm.config.command_blacklist_store import _DEFAULT_RULES
    rule = next(p for p, d in _DEFAULT_RULES if "递归删除根" in d)
    rx = re.compile(rule)
    for c in ["rm -rf ./build", "rm -rf target", "rm -rf node_modules",
              "rm -f /tmp/x.log", "rm file.txt", "rm -rf /workspace/sub",
              "rm -rf /home/user/proj/build", "rm -rf /etc/nginx/sites",
              "rm --force /tmp/x", "rm -v /tmp/y", "ls -r /etc"]:
        assert not rx.search(c), f"不应拦截: {c}"


# ─────────────────────────── F5/#85 密钥严重度 ───────────────────────────

def test_85_heuristic_secrets_stay_high_not_promoted():
    """★对抗双复核裁定提级 CRITICAL 处方过激已撤销★：ECC 分级契约(HIGH=warn/CRITICAL=block)
    刻意保留——提级会误报 RuoYi 基线 `CSRF_TOKEN = "csrf_token"`(常量名非密钥)。改的是 coding_
    standards 措辞（对齐承诺），severity 维持 HIGH。"""
    from swarm.worker.security_scan import _SECRET_PATTERNS, Severity
    sev = {n: s for n, _, s in _SECRET_PATTERNS}
    for n in ("Slack Token", "Google API Key", "Stripe Key", "Generic Secret Assignment"):
        assert sev[n] == Severity.HIGH, f"{n} 应保持 HIGH（ECC 分级契约 + 防误报）"


def test_85_csrf_constant_not_falsely_critical():
    """RuoYi 基线 `CSRF_TOKEN = "csrf_token"`（常量名非密钥）命中 Generic Secret 但只 HIGH（留痕
    不 block），绝不因提级 CRITICAL 冤杀阻断合法基线。"""
    from swarm.worker.security_scan import _SECRET_PATTERNS, Severity, scan_text_for_secrets
    hits = scan_text_for_secrets('public static final String CSRF_TOKEN = "csrf_token";')
    # 命中（heuristic），但 severity 是 HIGH（默认 critical 阈值不阻断）
    assert any("Generic Secret" in n for n, _ in hits)
    sev = {n: s for n, _, s in _SECRET_PATTERNS}
    assert sev["Generic Secret Assignment"] == Severity.HIGH


def test_85_coding_standards_wording_honest():
    """coding_standards 措辞不再过度承诺"全部阻断"，如实区分结构化(阻断)/通用赋值式(留痕)。"""
    from swarm.worker.coding_standards import _CORE_RULES
    secret_rule = next(r for r in _CORE_RULES if "硬编码密钥" in r)
    assert "留痕" in secret_rule and "阻断" in secret_rule


# ─────────────────────────── F4/#90 dep_legality 不猜 ───────────────────────────

def test_90_no_rewrite_without_strict_root_evidence():
    from swarm.worker.dep_legality import _resolve_prefixed_member
    # 外部依赖 jackson-core，workspace 有 core 成员，但 root_name=ruoyi（strict 不命中）→ 不改名
    r = _resolve_prefixed_member("jackson-core", {"core"}, root_name="ruoyi")
    assert r is None


def test_90_strict_root_match_still_resolves():
    from swarm.worker.dep_legality import _resolve_prefixed_member
    # ruoyi-common 恰是 {root}-{member} 强证据 → 还原为 common
    r = _resolve_prefixed_member("ruoyi-common", {"common"}, root_name="ruoyi")
    assert r == "common"


def test_90_root_name_none_returns_none_accepted_tradeoff():
    """★对抗复核记录的取舍★：root_name 探测失败(None)时一律 None（交回 prune）——刻意保守方向
    （误接外部依赖当内部→manifest 崩塌连坐 reactor 比缺内部依赖更毒）。此窗口若 root pom
    artifactId 抽取失败可能漏还原真兄弟，靠 root_name 抽取健壮性另行兜底，本函数不猜。"""
    from swarm.worker.dep_legality import _resolve_prefixed_member
    # 即便只有单一候选，root_name=None 也不还原（不猜前缀是否工程前缀）
    assert _resolve_prefixed_member("alarm-core", {"core"}, root_name=None) is None


# ─────────────────────────── F2/#88 池 forget temp 记账 ───────────────────────────

def test_88_forget_temp_does_not_decrement_borrowed():
    from swarm.worker.sandbox_pool import HotSandboxPool
    pool = HotSandboxPool(manager=MagicMock())
    pool._borrowed = 3
    # 模拟 temp 沙箱：有 created_at、在 _temp_sids、不在 idle
    pool._created_at["temp-1"] = 123.0
    pool._temp_sids.add("temp-1")
    pool.forget("temp-1")
    assert pool._borrowed == 3, "temp 沙箱 forget 不得递减 borrowed"


def test_88_forget_borrowed_nontemp_decrements():
    from swarm.worker.sandbox_pool import HotSandboxPool
    pool = HotSandboxPool(manager=MagicMock())
    pool._borrowed = 3
    pool._created_at["sb-1"] = 123.0   # 非 temp、不在 idle → 曾借出
    pool.forget("sb-1")
    assert pool._borrowed == 2


# ─────────────────────────── F3/#89 健康探针复用 manager ───────────────────────────

def test_89_health_check_reuses_manager_hardened_probe():
    from swarm.worker.sandbox_pool import HotSandboxPool
    mgr = MagicMock()
    mgr.health_check = MagicMock(return_value=True)   # manager 加固探针（含 504 重试）
    pool = HotSandboxPool(manager=mgr)
    assert pool._health_check(object()) is True
    mgr.health_check.assert_called_once()   # 走了加固路径，非单次 echo


def test_89_falls_back_to_echo_when_no_manager_probe():
    from swarm.worker.sandbox_pool import HotSandboxPool
    mgr = MagicMock(spec=["run_command"])   # 无 health_check
    mgr.run_command = MagicMock(return_value=MagicMock(success=True))
    pool = HotSandboxPool(manager=mgr)
    assert pool._health_check(object()) is True
    mgr.run_command.assert_called_once()


# ─────────────────────────── F1/#87 git flock 无锁可观测 ───────────────────────────

def test_87_flock_failure_warns_every_time_and_records_unlocked(caplog):
    import logging

    from swarm.worker import git_flock
    lock = git_flock._ProjectGitFlock.__new__(git_flock._ProjectGitFlock)
    fake_fcntl = MagicMock()
    fake_fcntl.LOCK_EX = 2
    fake_fcntl.flock = MagicMock(side_effect=OSError("ENOLCK"))
    lock._lock_f = object()
    lock._fcntl = fake_fcntl
    with caplog.at_level(logging.WARNING, logger="swarm.worker.git_flock"), \
         patch.object(git_flock.time, "sleep", lambda *_a: None):
        lock.__enter__()
    assert lock._locked is False
    assert any("flock(LOCK_EX) 失败" in r.message for r in caplog.records)


# #116-B1（复读 abort 161s）已推迟——取证显示 abort 点 coverage=0.41（高于 0.25 阈值），延迟根因
# 是【跨窗持续】而非单窗 coverage 未达标，正确修法=对同一 needle 设 wall-time 上限（需把时间穿进
# detector+调用方，较侵入），非单窗内 char 计数旁路。correctness 已兜住（不假过），throughput 优化，
# 处方需克制，不冒险改动 correctness 敏感组件。B2 已由 #108 签名熔断覆盖，B3 部分由 #89 覆盖。


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
