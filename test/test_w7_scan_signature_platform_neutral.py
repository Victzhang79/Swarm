"""#29-2 W-7（v0.9.74 引入的回归）：全树扫描签名必须真的会变，缓存才会失效。

★原缺陷★ `_scan_sig_command` 按 `sys.platform`（**本机**）选 `stat` 语法，而
`_run_check_split` 是**沙箱优先**的（沙箱=Linux）⇒ 开发机 macOS 上生成 BSD 语法却在 Linux
执行 ⇒ `stat` 报错被 `2>/dev/null` 吞 ⇒ xargs 空输入 ⇒ 签名恒为 `4294967295 0`
⇒ 缓存**永不失效**。兜底"签名拿不到→不缓存"只认**空串**，而空输入的 cksum 是**非空常量**
⇒ 兜底被完整绕过。5 个消费者全是 JVM 全树符号扫描且在 repair 收敛循环内：
`_attempt_symbol_repair` 拿过期频次表做改名决策，#114 反震荡判据前提失效。

★为什么既有 test_a7_scan_cache.py 三条全绿却漏了它★（血规 10② 的教科书实例）
那三条把 `_run_check_split` 整个 monkeypatch 掉，喂 `"111 2222"` / `"samesig"` 这类**生产
代码从不产生的手工取值**——签名命令本身对不对、在目标平台跑不跑得通，它们一个字都没测。
故本文件的锁点是：**用真命令、真文件系统**跑签名，断它随文件变化；以及空 cksum 必须当
无签名。这样"签名命令写错语法/换平台失效"这一整类才会红。
"""

from __future__ import annotations

import subprocess

import pytest
from swarm.worker import l1_pipeline as L


def _real_sig(project_path: str) -> str:
    """按生产同一条命令、在本机 shell 真跑一次签名（不 monkeypatch 任何东西）。"""
    proc = subprocess.run(L._scan_sig_command(), cwd=project_path, shell=True,
                          capture_output=True, text=True, timeout=30)
    return (proc.stdout or "").strip()


# ── A. 真命令 × 真文件系统：签名必须有区分力 ──────────────────────────────

class TestRealSignatureDiscriminates:
    """★这一组才是能抓住 W-7 的锁★：不 fake `_run_check_split`，跑真命令。"""

    def _mk(self, root):
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)
        (src / "A.java").write_text("class A {}\n", encoding="utf-8")
        (src / "B.java").write_text("class B {}\n", encoding="utf-8")
        return src

    def test_signature_is_not_the_empty_cksum_on_a_real_tree(self, tmp_path):
        """前提锁：树里有源文件时签名必须不是空 cksum（是空就说明命令整条没跑通——
        这正是 W-7 在 Linux 沙箱里的实际取值）。"""
        self._mk(tmp_path)
        sig = _real_sig(str(tmp_path))
        assert sig, "签名为空串：命令没产出任何输出"
        assert sig != L._EMPTY_CKSUM, (
            f"签名等于空 cksum({L._EMPTY_CKSUM}) ⇒ 签名命令在本平台没跑通 "
            f"⇒ 缓存永不失效（W-7 原病）")

    def test_signature_changes_when_content_changes(self, tmp_path):
        src = self._mk(tmp_path)
        before = _real_sig(str(tmp_path))
        (src / "A.java").write_text("class A { void x() {} }\n", encoding="utf-8")
        after = _real_sig(str(tmp_path))
        assert before != after, (
            "改了源文件内容而签名不变 ⇒ 缓存不会失效 ⇒ repair 拿过期符号表做改名决策")

    def test_signature_changes_when_file_added(self, tmp_path):
        src = self._mk(tmp_path)
        before = _real_sig(str(tmp_path))
        (src / "C.java").write_text("class C {}\n", encoding="utf-8")
        assert before != _real_sig(str(tmp_path)), "新增源文件签名必须变"

    def test_signature_changes_when_file_deleted(self, tmp_path):
        src = self._mk(tmp_path)
        before = _real_sig(str(tmp_path))
        (src / "B.java").unlink()
        assert before != _real_sig(str(tmp_path)), "删除源文件签名必须变"

    def test_signature_changes_when_file_renamed(self, tmp_path):
        """改名不改内容：符号表里的类→文件映射变了，签名必须能区分。"""
        src = self._mk(tmp_path)
        before = _real_sig(str(tmp_path))
        (src / "B.java").rename(src / "B2.java")
        assert before != _real_sig(str(tmp_path)), "改名签名必须变"

    def test_signature_stable_when_nothing_changes(self, tmp_path):
        """反向：什么都不动，签名必须稳定（否则缓存永不命中，A7 省预算的初衷落空）。"""
        self._mk(tmp_path)
        assert _real_sig(str(tmp_path)) == _real_sig(str(tmp_path))

    def test_signature_ignores_non_jvm_files(self, tmp_path):
        """口径锁：只认 .java/.kt/.scala —— 改 README 不该让 JVM 符号表缓存失效。"""
        self._mk(tmp_path)
        before = _real_sig(str(tmp_path))
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
        assert before == _real_sig(str(tmp_path))

    @pytest.mark.parametrize("ext", ["kt", "scala"])
    def test_signature_covers_kotlin_and_scala(self, tmp_path, ext):
        src = self._mk(tmp_path)
        before = _real_sig(str(tmp_path))
        (src / f"D.{ext}").write_text("class D\n", encoding="utf-8")
        assert before != _real_sig(str(tmp_path)), f".{ext} 未纳入签名"

    def test_empty_tree_yields_the_empty_cksum(self, tmp_path):
        """把"空树"这个事实钉住：它与"命令失败"同值 ⇒ 下面那组必须当无证据处理。"""
        assert _real_sig(str(tmp_path)) == L._EMPTY_CKSUM


# ── B. 平台中立：不得再按本机平台分叉 ────────────────────────────────────

def test_signature_command_has_no_platform_branch(monkeypatch):
    """★根因锁★：签名命令在任何 `sys.platform` 下都必须**逐字相同**。

    断的是"接线事实/单一事实源"（本机平台不参与决策），不是断命令里有什么字面量——
    否则就成了焊死实现细节的守卫测试（纪律 6）。
    """
    seen = set()
    for plat in ("darwin", "linux", "win32", "freebsd"):
        monkeypatch.setattr(L.sys, "platform", plat)
        seen.add(L._scan_sig_command())
    assert len(seen) == 1, (
        f"签名命令随本机平台变化 ⇒ 与【执行环境】（沙箱优先=Linux）错配的老路复发: {seen}")


def test_signature_command_uses_no_stat(monkeypatch):
    """`stat` 的格式化选项是 GNU/BSD 分叉的根源；平台中立方案不该再依赖它。

    这条与上一条不重叠：上一条只保证"不随平台变"，一个**写死单一 stat 语法**的实现也能
    通过它（然后在另一侧静默失效）。这条盯的是分叉源本身。
    """
    assert "stat" not in L._scan_sig_command()


# ── C. 空 cksum 必须当无签名（兜底被绕过的那一环）────────────────────────

class TestEmptyCksumTreatedAsNoSignature:

    def test_empty_cksum_disables_caching(self, monkeypatch):
        """★W-7 的咬合点★：签名为空 cksum（命令失效 or 空树）时**不得缓存**。
        原实现只判空串 ⇒ 这个非空常量一路通过 ⇒ 缓存永不失效。"""
        L._SCAN_CACHE.clear()
        calls = {"scan": 0}
        sig_cmd = L._scan_sig_command()

        def fake_run(cmd, path, timeout=60):
            if cmd == sig_cmd:
                return (0, L._EMPTY_CKSUM, "")
            calls["scan"] += 1
            return (0, "SYMBOLS", "")

        monkeypatch.setattr(L, "_run_check_split", fake_run)
        L._cached_scan("g", "/p")
        L._cached_scan("g", "/p")
        assert calls["scan"] == 2, (
            "空 cksum 被当成有效签名 → 缓存命中 → 永不失效（W-7 原病）")

    def test_empty_cksum_with_trailing_whitespace_also_disabled(self, monkeypatch):
        """真实 shell 输出带尾换行；判据必须在 strip 之后（否则这一档从旁路溜过）。"""
        L._SCAN_CACHE.clear()
        calls = {"scan": 0}
        sig_cmd = L._scan_sig_command()

        def fake_run(cmd, path, timeout=60):
            if cmd == sig_cmd:
                return (0, f"  {L._EMPTY_CKSUM}\n", "")
            calls["scan"] += 1
            return (0, "SYMBOLS", "")

        monkeypatch.setattr(L, "_run_check_split", fake_run)
        L._cached_scan("g", "/p")
        L._cached_scan("g", "/p")
        assert calls["scan"] == 2

    def test_real_signature_still_enables_caching(self, monkeypatch):
        """反向前提锁：正常签名必须照旧命中缓存，否则 A7 的省预算意义没了
        （只验"不缓存"会让把缓存整条拆掉的改动也全绿）。"""
        L._SCAN_CACHE.clear()
        calls = {"scan": 0}
        sig_cmd = L._scan_sig_command()

        def fake_run(cmd, path, timeout=60):
            if cmd == sig_cmd:
                return (0, "1230344305 27", "")
            calls["scan"] += 1
            return (0, "SYMBOLS", "")

        monkeypatch.setattr(L, "_run_check_split", fake_run)
        L._cached_scan("g", "/p")
        L._cached_scan("g", "/p")
        assert calls["scan"] == 1, "正常签名下第二次应命中缓存"

    def test_signature_change_invalidates(self, monkeypatch):
        L._SCAN_CACHE.clear()
        calls = {"scan": 0}
        sig_cmd = L._scan_sig_command()
        sig = ["1230344305 27"]

        def fake_run(cmd, path, timeout=60):
            if cmd == sig_cmd:
                return (0, sig[0], "")
            calls["scan"] += 1
            return (0, "SYMBOLS", "")

        monkeypatch.setattr(L, "_run_check_split", fake_run)
        L._cached_scan("g", "/p")
        sig[0] = "2008410789 27"
        L._cached_scan("g", "/p")
        assert calls["scan"] == 2


# ── D. 端到端：真命令 + 真缓存，改文件必须重扫 ────────────────────────────

def test_end_to_end_cache_invalidates_on_real_file_change(tmp_path, monkeypatch):
    """★整条链的锁★：只 fake 被缓存的那条扫描命令，签名走**真命令真文件系统**。

    W-7 下这条必红：签名恒定 ⇒ 改了文件仍命中缓存 ⇒ scan 只跑一次。
    """
    L._SCAN_CACHE.clear()
    src = tmp_path / "src"
    src.mkdir()
    (src / "A.java").write_text("class A {}\n", encoding="utf-8")

    sig_cmd = L._scan_sig_command()
    calls = {"scan": 0}
    real_split = L._run_check_split

    def hybrid(cmd, path, timeout=60):
        if cmd == sig_cmd:
            return real_split(cmd, path, timeout=timeout)   # 真签名
        calls["scan"] += 1
        return (0, "SYMBOLS", "")

    monkeypatch.setattr(L, "_sandbox_ctx", lambda: None)    # 本地模式，真跑 shell
    monkeypatch.setattr(L, "_run_check_split", hybrid)

    L._cached_scan("grep -r symbols .", str(tmp_path))
    L._cached_scan("grep -r symbols .", str(tmp_path))
    assert calls["scan"] == 1, "文件未变 → 应命中缓存"

    (src / "A.java").write_text("class A { void x() {} }\n", encoding="utf-8")
    L._cached_scan("grep -r symbols .", str(tmp_path))
    assert calls["scan"] == 2, (
        "源文件改了却仍命中缓存 ⇒ repair 会拿过期符号表做改名决策（W-7 原病）")
