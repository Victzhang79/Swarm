"""R65E10-T1（round65e10 FAILED@执行期 四路定案·死因①）：verify_command 已 `-pl <mod>`
但缺 `-am` → 解析不到 reactor 兄弟（ruoyi-common:4.8.3）→ 假阴性烧正确代码 → st-1 head-of-line
连坐全 92 子任务。

死因铁证（swarm.log:2212/2497/2818 三度判死，末次 escalate）：
- L1.2.1 build 闸 `mvn -pl ruoyi-alarm-interface -am -q compile`（带 -am）→ 过（compile ok）
- plan 自撰 verify `mvn compile -pl ruoyi-alarm-interface -q`（【无 -am】）→ Could not find artifact
  com.ruoyi:ruoyi-common:jar:4.8.3 → verify_failed。

根：`_scope_maven_command`（l1_pipeline.py:3174）与 `_reactorize_verify_command` cd-branch（:3294）都把
"已含 -pl"当作"已 reactor 感知"原样返回——R65E8-T1 只治了 `cd <mod> && mvn` 裸形，漏了【已 -pl 缺 -am】形。

治：共享纯函数 _ensure_reactor_am——命令已 -pl、目标需上游产物(compile/test/package/verify/install/deploy)、
且无 -am → 在 -pl <targets> 后注入 -am（与 build/test 通道对称）。validate/clean/已 -am → 原样。
"""
from __future__ import annotations

from swarm.worker.l1_pipeline import _ensure_reactor_am, _scope_maven_command


# ── 纯函数 _ensure_reactor_am ──
def test_injects_am_for_compile_pl_without_am():
    """★RED 核★ round65e10 st-1 死因命令：已 -pl、compile、无 -am → 注入 -am。"""
    out = _ensure_reactor_am("mvn compile -pl ruoyi-alarm-interface -q")
    assert "-am" in out
    assert out == "mvn compile -pl ruoyi-alarm-interface -am -q", out


def test_am_inserted_right_after_pl_targets():
    """-am 紧跟 -pl <targets> 之后（reactor 语义正确位置）。"""
    out = _ensure_reactor_am("mvn -pl mod-a test -q")
    assert out == "mvn -pl mod-a -am test -q", out


def test_multi_module_pl_list_gets_am():
    out = _ensure_reactor_am("mvn package -pl mod-a,mod-b -q")
    assert out == "mvn package -pl mod-a,mod-b -am -q", out


def test_no_double_am_when_present():
    """已有 -am → 逐字原样（不重复注入）。"""
    cmd = "mvn compile -pl ruoyi-alarm-interface -am -q"
    assert _ensure_reactor_am(cmd) == cmd


def test_long_form_also_make_not_doubled():
    """--also-make 长形也视作已带 -am → 原样。"""
    cmd = "mvn compile -pl mod-a --also-make -q"
    assert _ensure_reactor_am(cmd) == cmd


def test_validate_goal_no_am():
    """validate=模块级弱校验，不需上游产物 → 不加 -am（守 P0-B 不连坐 sibling）。"""
    cmd = "mvn validate -pl mod-a -q"
    assert _ensure_reactor_am(cmd) == cmd


def test_clean_goal_no_am():
    cmd = "mvn -pl mod-a clean"
    assert _ensure_reactor_am(cmd) == cmd


def test_no_pl_untouched():
    """无 -pl → 本 helper 不动（-pl 推导交 _scope_maven_command 主逻辑）。"""
    cmd = "mvn compile -q"
    assert _ensure_reactor_am(cmd) == cmd


def test_non_mvn_untouched():
    cmd = "grep -q 'lombok' pom.xml"
    assert _ensure_reactor_am(cmd) == cmd


def test_test_compile_hyphen_goal_gets_am():
    """test-compile 含 'compile' 子串——需上游 → 注入 -am（与既有 needs_upstream 口径一致）。"""
    out = _ensure_reactor_am("mvn test-compile -pl mod-a -q")
    assert out == "mvn test-compile -pl mod-a -am -q", out


# ── ★复核 HIGH 回归锁★ 模块名含 goal 词元子串不得假触发 -am（守 P0-B） ──
def test_validate_module_name_contains_test_no_am():
    """`mvn validate -pl ruoyi-quartz-test` —— 模块名含 'test' 子串，但 goal 是 validate →
    绝不加 -am（否则连坐 sibling 违 P0-B）。复核 HIGH：goal 判定须先剥 -pl 段。"""
    cmd = "mvn validate -pl ruoyi-quartz-test -q"
    assert _ensure_reactor_am(cmd) == cmd, "validate + 模块名含 test 不得被误加 -am"


def test_validate_module_name_contains_install_no_am():
    cmd = "mvn validate -pl ruoyi-install-helper -q"
    assert _ensure_reactor_am(cmd) == cmd


def test_validate_module_name_contains_package_no_am():
    cmd = "mvn clean -pl ruoyi-package-scanner"
    assert _ensure_reactor_am(cmd) == cmd


def test_real_compile_with_test_named_module_still_gets_am():
    """真 compile + 模块名含 test → 仍应加 -am（goal 是真 compile，非模块名假触发）。"""
    out = _ensure_reactor_am("mvn compile -pl ruoyi-quartz-test -q")
    assert out == "mvn compile -pl ruoyi-quartz-test -am -q", out


def test_am_injection_logged(monkeypatch):
    """★复核 MED 回归锁★ 补齐 -am 必留 [L1.3.5] R65E10-T1 日志（审计可见）。
    直接捕获模块 logger（不依赖全局 logging 配置，避免套件级 isolation 抖动）。"""
    import swarm.worker.l1_pipeline as _lp
    msgs = []
    monkeypatch.setattr(_lp.logger, "info", lambda m, *a, **k: msgs.append(m % a if a else m))
    _ensure_reactor_am("mvn compile -pl ruoyi-alarm-interface -q")
    assert any("R65E10-T1" in m for m in msgs), f"补齐必留痕: {msgs}"


def test_no_op_not_logged(monkeypatch):
    """未改写（已 -am）→ 不产生补齐日志（避免噪声）。"""
    import swarm.worker.l1_pipeline as _lp
    msgs = []
    monkeypatch.setattr(_lp.logger, "info", lambda m, *a, **k: msgs.append(m % a if a else m))
    _ensure_reactor_am("mvn compile -pl mod -am -q")
    assert not any("R65E10-T1" in m for m in msgs)


# ── _scope_maven_command：已 -pl 分支现在补 -am（回归 round65e10 死因） ──
def test_scope_maven_pl_compile_now_gets_am(tmp_path):
    """★死因回归锁★ _scope_maven_command 此前 `-pl in command → 原样`，漏 -am。
    现应对已 -pl 的 upstream 目标补 -am（无需读模块=纯字符串，project_path 任意）。"""
    out = _scope_maven_command(
        "mvn compile -pl ruoyi-alarm-interface -q", str(tmp_path), [])
    assert out == "mvn compile -pl ruoyi-alarm-interface -am -q", out


def test_scope_maven_pl_validate_stays(tmp_path):
    """已 -pl 的 validate → 仍原样（不加 -am）。"""
    cmd = "mvn validate -pl mod-a -q"
    assert _scope_maven_command(cmd, str(tmp_path), []) == cmd


def test_scope_maven_pl_with_am_unchanged(tmp_path):
    cmd = "mvn compile -pl mod-a -am -q"
    assert _scope_maven_command(cmd, str(tmp_path), []) == cmd


def test_scope_maven_non_mvn_unchanged(tmp_path):
    cmd = "echo hi"
    assert _scope_maven_command(cmd, str(tmp_path), []) == cmd
