"""A7 治本(round11)：只读全树符号/包扫描按文件签名缓存，省 VERIFYING/PRODUCING 重复 60-120s。

正确性核心：签名=.java/.kt size+mtime，任一源文件变动→签名变→自动失效重扫，绝不拿过期符号表。
"""
from __future__ import annotations

from swarm.worker import l1_pipeline as L


def test_cached_scan_reuses_when_files_unchanged(monkeypatch):
    L._SCAN_CACHE.clear()
    calls = {"sig": 0, "scan": 0}
    sig = ["111 2222"]

    def fake_run(cmd, path, timeout=60):
        if cmd == L._SCAN_SIG_CMD:
            calls["sig"] += 1
            return (0, sig[0], "")
        calls["scan"] += 1
        return (0, "SCAN_OUTPUT", "")

    monkeypatch.setattr(L, "_run_check_split", fake_run)
    r1 = L._cached_scan("grep symbols", "/proj", timeout=60)
    r2 = L._cached_scan("grep symbols", "/proj", timeout=60)
    assert r1 == r2 == (0, "SCAN_OUTPUT", "")
    assert calls["scan"] == 1, "文件未变 → 第二次应命中缓存，不重扫"
    # 源文件变动 → 签名变 → 必须重扫（不返回陈旧符号表）
    sig[0] = "999 8888"
    L._cached_scan("grep symbols", "/proj", timeout=60)
    assert calls["scan"] == 2, "签名变(文件改了) → 必须重扫"


def test_cached_scan_keyed_by_cmd_and_path(monkeypatch):
    L._SCAN_CACHE.clear()
    scans = []

    def fake_run(cmd, path, timeout=60):
        if cmd == L._SCAN_SIG_CMD:
            return (0, "samesig", "")
        scans.append((cmd, path))
        return (0, "o", "")

    monkeypatch.setattr(L, "_run_check_split", fake_run)
    L._cached_scan("cmdA", "/p1"); L._cached_scan("cmdA", "/p1")  # 命中
    L._cached_scan("cmdB", "/p1")  # 不同 cmd → miss
    L._cached_scan("cmdA", "/p2")  # 不同 path → miss
    assert scans == [("cmdA", "/p1"), ("cmdB", "/p1"), ("cmdA", "/p2")], \
        "缓存按 (path, cmd) 分键，同状态同键才命中"


def test_cached_scan_no_cache_when_sig_unavailable(monkeypatch):
    """签名拿不到(空)→ 不缓存、每次照常扫描（安全兜底，绝不返回可能陈旧结果）。"""
    L._SCAN_CACHE.clear()
    calls = {"scan": 0}

    def fake_run(cmd, path, timeout=60):
        if cmd == L._SCAN_SIG_CMD:
            return (0, "", "")
        calls["scan"] += 1
        return (0, "x", "")

    monkeypatch.setattr(L, "_run_check_split", fake_run)
    L._cached_scan("g", "/p")
    L._cached_scan("g", "/p")
    assert calls["scan"] == 2, "无签名 → 不缓存 → 每次都扫"
